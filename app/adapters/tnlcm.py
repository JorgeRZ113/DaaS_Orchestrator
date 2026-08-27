import json
import logging
import asyncio
import ast
import re
from pathlib import Path
from typing import Any, Callable

import httpx

from app.adapters.http import (
    log_http_response,
    resolve_examples_path,
    response_error_detail,
)
from app.core.config import settings
from app.adapters.tnlcm_schemas import ActivateRequest, TokenPair
from app.domain.descriptor import InfrastructureConfig
from app.core import retry
from app.observability.telemetry import telemetry

logger = logging.getLogger(__name__)

# TNLCM timing constants (kept local to this adapter)
TNLCM_REQUEST_TIMEOUT = 60
TNLCM_LOGIN_TIMEOUT_SECONDS = 20
TNLCM_ACTIVATE_REDEPLOY_MAX_ATTEMPTS = 1
TNLCM_REDEPLOY_DELAY = 5
TNLCM_RECOVERY_DESTROY_DELAY = 0
TNLCM_LEGACY_NON_RETRYABLE_STATUS_CODES = {400, 404, 422}
TNLCM_LEGACY_ERROR_HINT = "Revise lo indicado por el mensaje de error."

# Vocabulario que TNLCM expone en el campo `state` de GET /trial-networks/{tn_id}.
# Se usa para reconciliar cuando create/activate devuelve 400/409 porque la TN ya
# existe: el estado real decide qué fase resta (activar / levantar VPN) en vez de
# abortar la ejecución. Comparaciones siempre en minúsculas.
TN_STATE_CREATED = frozenset({"created", "validated"})
TN_STATE_ACTIVATED = frozenset({"activated"})
TN_STATE_TERMINAL = frozenset({"failed", "destroyed", "purged"})

# Códigos con los que TNLCM responde cuando la TN ya existe en algún estado.
TNLCM_ALREADY_EXISTS_STATUS_CODES = {400, 409}


# Token storage in memory (populated by login endpoint)
_tnlcm_access_token: str | None = None
_tnlcm_refresh_token: str | None = None


# Gancho opcional con el que el llamante se entera de lo que pasa MIENTRAS pasa.
# Existe porque los reintentos solo dejaban rastro en `telemetry.log_event`, que no
# retiene nada en memoria: sin esto, "activate va por el intento 2 de 3" no hay
# forma de contarlo desde ningun endpoint. El adaptador solo ve un callable opaco,
# asi que la dependencia sigue yendo de `services` a `adapters` y no al reves.
OnProgress = Callable[[str], None]


class _ActivateNoSuchFileError(Exception):
    """Raised when TNLCM activate fails due to missing file on backend side."""


class _ActivateRetryExhaustedError(Exception):
    """Raised when activate retries are exhausted for retryable errors."""


class TnReportDownloadError(RuntimeError):
    """Base error for TNLCM report download failures."""


class TnNotFoundError(TnReportDownloadError):
    """Raised when report download is requested for a non-existent TN."""


class TnNotActivatedError(TnReportDownloadError):
    """Raised when report download is requested before TN activation."""


class TnReportGenerationError(TnReportDownloadError):
    """Raised when TNLCM fails to generate/read the report artifact."""


class TnStatusBadRequestError(RuntimeError):
    """Raised when TN status request must be treated as a 400 client error."""


# Nombre del evento de telemetria segun la causa del reintento. Se resuelve por
# isinstance y no por `type(exc)`: httpx nunca lanza `TimeoutException` a secas,
# lanza subclases (`ReadTimeout`, `ConnectTimeout`...). El orden importa, porque
# `TimeoutException` es a su vez subclase de `TransportError`.
_ACTIVATE_RETRY_EVENTS: tuple[tuple[type[BaseException], str], ...] = (
    (httpx.HTTPStatusError, "tnlcm.activate.retry"),
    (httpx.TimeoutException, "tnlcm.activate.timeout"),
    (httpx.TransportError, "tnlcm.activate.transport_error"),
)


def _activate_retry_event(error: BaseException) -> str:
    for error_type, event in _ACTIVATE_RETRY_EVENTS:
        if isinstance(error, error_type):
            return event
    return "tnlcm.activate.retry"


async def _activate_with_backoff(
    request_call,
    tn_id: str,
    endpoint_label: str,
    execution_id: str | None = None,
    on_progress: OnProgress | None = None,
) -> None:
    """Activa una TN reintentando los fallos transitorios de TNLCM.

    El bucle y los retardos los pone `retry.TNLCM_ACTIVATE`; aqui solo queda lo
    propio del adaptador: cuando NO reintentar (`_veto`), que telemetria emitir en
    cada reintento (`_on_retry`) y como traducir el fallo final a las excepciones
    que entiende la ruta de recuperacion de `deploy_trial_network`.

    `on_progress` recibe una linea por reintento. Es lo unico que saca ese hecho
    del fichero de log: la fase lo cablea al `message` de la ejecucion para que se
    pueda leer desde la API mientras el despliegue sigue en curso.
    """
    policy = retry.TNLCM_ACTIVATE

    activate_timer = telemetry.start_timer("tnlcm", "activate", execution_id=execution_id)
    activate_timer.start()
    telemetry.log_event(
        "info",
        "tnlcm.activate.started",
        service="tnlcm",
        operation="activate",
        execution_id=execution_id,
        tn_id=tn_id,
    )

    async def _call() -> httpx.Response:
        telemetry.increment_counter("tnlcm_activate_attempts", labels={"service": "tnlcm"})
        response = await request_call()
        log_http_response("TNLCM", response)
        response.raise_for_status()
        return response

    def _veto(error: BaseException) -> bool:
        # "no such file or directory" es un fallo permanente del facility: la TN
        # necesita destroy+purge+redeploy, no otro intento.
        return isinstance(error, httpx.HTTPStatusError) and _is_no_such_file_error(error.response)

    def _on_retry(attempt: retry.Attempt) -> None:
        telemetry.increment_counter(
            "retries_total", labels={"service": "tnlcm", "operation": "activate"}
        )
        logger.warning(
            "TNLCM %s activate failed for tn_id=%s (%s). Retry %s/%s in %ss.",
            endpoint_label,
            tn_id,
            attempt.error,
            attempt.number + 1,
            attempt.max_attempts,
            attempt.delay_seconds,
        )
        extra_fields: dict[str, Any] = {}
        if isinstance(attempt.error, httpx.HTTPStatusError):
            extra_fields["status_code"] = attempt.error.response.status_code
        else:
            extra_fields["error"] = str(attempt.error)
        telemetry.log_event(
            "warning",
            _activate_retry_event(attempt.error),
            service="tnlcm",
            operation="activate",
            execution_id=execution_id,
            tn_id=tn_id,
            attempt=attempt.number,
            next_retry_in_seconds=attempt.delay_seconds,
            **extra_fields,
        )
        if on_progress is not None:
            # Best-effort: informar del progreso no puede tumbar un despliegue que
            # por lo demas iba a reintentar con normalidad.
            try:
                on_progress(
                    f"Activating TN {tn_id}: attempt {attempt.number}/"
                    f"{attempt.max_attempts} failed ({attempt.error}); retrying in "
                    f"{attempt.delay_seconds:g} s"
                )
            except Exception:
                logger.debug("on_progress hook failed", exc_info=True)

    def _fail(event: str, **fields: Any) -> None:
        activate_timer.stop(status="error")
        telemetry.log_event(
            "error",
            event,
            service="tnlcm",
            operation="activate",
            execution_id=execution_id,
            tn_id=tn_id,
            **fields,
        )
        telemetry.increment_counter(
            "errors_total", labels={"service": "tnlcm", "operation": "activate"}
        )

    try:
        await policy.run(_call, veto=_veto, on_retry=_on_retry)
    except httpx.HTTPStatusError as exc:
        log_http_response("TNLCM", exc.response)
        status_code = exc.response.status_code

        if _is_no_such_file_error(exc.response):
            _fail("tnlcm.activate.failed", error="no_such_file")
            raise _ActivateNoSuchFileError() from exc

        if status_code in policy.retry_statuses:
            detail = response_error_detail(exc.response)
            _fail("tnlcm.activate.exhausted", status_code=status_code, error=detail)
            raise _ActivateRetryExhaustedError(
                (
                    f"TNLCM {endpoint_label} activate exhausted retries for tn_id={tn_id} "
                    f"(HTTP {status_code}). Backend error: {detail or 'unknown'}"
                )
            ) from exc

        # Se propaga crudo: el llamante mira el 404/405 para caer al endpoint legacy.
        _fail("tnlcm.activate.failed", status_code=status_code)
        raise
    except httpx.TimeoutException as exc:
        _fail("tnlcm.activate.timeout.exhausted")
        raise _ActivateRetryExhaustedError(
            f"TNLCM {endpoint_label} activate exhausted retries for tn_id={tn_id} due to timeout"
        ) from exc
    except httpx.TransportError as exc:
        _fail("tnlcm.activate.transport_error.exhausted", error=str(exc))
        raise _ActivateRetryExhaustedError(
            f"TNLCM {endpoint_label} activate exhausted retries for tn_id={tn_id} "
            "due to transport errors"
        ) from exc

    activate_timer.stop(status="success")
    telemetry.log_event(
        "info",
        "tnlcm.activate.completed",
        service="tnlcm",
        operation="activate",
        execution_id=execution_id,
        tn_id=tn_id,
    )
    telemetry.increment_counter(
        "tnlcm_activate_total", labels={"service": "tnlcm", "status": "success"}
    )


def _raise_legacy_create_error(response: httpx.Response | None) -> None:
    status_code = getattr(response, "status_code", "unknown")
    detail = response_error_detail(response) or "unknown"
    if isinstance(status_code, int) and status_code in TNLCM_LEGACY_NON_RETRYABLE_STATUS_CODES:
        raise RuntimeError(
            f"TNLCM /legacy (HTTP {status_code}): {detail}. {TNLCM_LEGACY_ERROR_HINT}"
        )
    raise RuntimeError(
        f"TNLCM /legacy failed (HTTP {status_code}): {detail}. {TNLCM_LEGACY_ERROR_HINT}"
    )


def _headers() -> dict[str, str]:
    """Build headers using the token stored in memory by /login."""
    token = _tnlcm_access_token.strip() if _tnlcm_access_token else ""
    if not token:
        raise ValueError("TNLCM access token is not loaded in memory. Call /login first.")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _json_headers() -> dict[str, str]:
    headers = _headers()
    headers["Content-Type"] = "application/json"
    return headers


def login_tnlcm_and_persist_token() -> str:
    """Login into TNLCM using env credentials and store access token in memory."""
    global _tnlcm_access_token, _tnlcm_refresh_token

    username = settings.tnlcm_user.strip()
    password = settings.tnlcm_password.strip()

    if not username or not password:
        raise ValueError("TNLCM_USER/TNLCM_PASSWORD are required in .env")

    paths = ["/api/v1/user/login"]

    with httpx.Client(timeout=TNLCM_LOGIN_TIMEOUT_SECONDS) as client:
        response_data: dict[str, Any] | None = None

        for path in paths:
            response = client.post(
                f"{settings.tnlcm_url}{path}",
                auth=(username, password),
                headers={"Accept": "application/json"},
            )

            # Login response includes tokens, avoid logging body.
            logger.info("TNLCM POST %s -> %s", response.request.url, response.status_code)

            if response.status_code == 404:
                continue

            response.raise_for_status()
            response_data = response.json()
            break

        if response_data is None:
            raise RuntimeError("No valid TNLCM login endpoint found")

    tokens = TokenPair.from_login_response(response_data)

    _tnlcm_access_token = tokens.access_token
    if tokens.refresh_token:
        _tnlcm_refresh_token = tokens.refresh_token

    logger.info("TNLCM token refreshed and stored in memory")
    return tokens.access_token


def _is_no_such_file_error(response: httpx.Response | None) -> bool:
    if response is None:
        return False

    body = ""
    try:
        body = response.text or ""
    except Exception:
        body = ""

    if not body:
        try:
            body = json.dumps(response.json())
        except Exception:
            body = ""

    return "no such file or directory" in body.lower()


def _extract_tn_id(data: dict[str, Any]) -> str | None:
    return data.get("tn_id")


def resolve_tn_id(infra: InfrastructureConfig) -> str:
    """Resuelve el tn_id efectivo de una infraestructura.

    TNLCM usa este identificador como clave primaria de la TN: coincide con el
    `tn_id` explícito de parameters o, en su defecto, con el nombre de la
    infraestructura. Se centraliza para poder consultar el estado de una TN ya
    existente sin depender de la respuesta del create (reconciliación).
    """
    return str(infra.parameters.get("tn_id") or infra.name)


def _extract_report_markdown(response: httpx.Response) -> str:
    """Normalize TNLCM report/download response into raw markdown text."""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            report = payload.get("report")
            if isinstance(report, str):
                return report
        if isinstance(payload, str):
            return payload
    except Exception:
        pass

    return response.text or ""


def summarize_trial_network_report(report_markdown: str) -> dict[str, Any]:
    """Extract key fields from TNLCM raw markdown report."""

    def _extract_component_blocks(raw_text: str) -> dict[str, str]:
        # Divide el report en bloques de componente. Cada componente empieza con
        # un header markdown: normalmente nivel 1 (`#`), pero TNLCM emite algunos
        # (p.ej. el vnet) como nivel 2 (`##`). Para ser resiliente a ese cambio
        # de formato se acepta también un header de nivel 2 como frontera, pero
        # SOLO si su sección contiene la frase "The component `...` has been ...
        # created": así se distingue un componente real de subsecciones internas
        # como "## ELCM BACKEND", "## Grafana" o "## Important information:". Se
        # ignora cualquier header dentro de bloques de código cercados (```).
        component_marker = re.compile(r"The component\s+`[^`]+`\s+has been", flags=re.IGNORECASE)
        lines = raw_text.splitlines()
        candidates: list[tuple[int, int, str]] = []
        in_fence = False

        for idx, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue

            match = re.match(r"^(#{1,2})\s+(.+)$", stripped)
            if match:
                candidates.append((idx, len(match.group(1)), match.group(2).strip()))

        # Selecciona qué headers candidatos son fronteras reales de componente:
        # todos los de nivel 1, y los de nivel 2 cuya sección marca un componente.
        headers: list[tuple[int, str]] = []
        for pos, (start_line, level, component_name) in enumerate(candidates):
            next_candidate = candidates[pos + 1][0] if pos + 1 < len(candidates) else len(lines)
            if level == 1:
                headers.append((start_line, component_name))
                continue
            section_text = "\n".join(lines[start_line + 1 : next_candidate])
            if component_marker.search(section_text):
                headers.append((start_line, component_name))

        blocks: dict[str, str] = {}
        for pos, (start_line, component_name) in enumerate(headers):
            end_line = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
            blocks[component_name] = "\n".join(lines[start_line + 1 : end_line]).strip()

        return blocks

    def _parse_interfaces(block: str) -> dict[str, str]:
        match = re.search(
            r"VM network interfaces.*?```(?:json)?\s*(.*?)```",
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return {}

        raw_obj = match.group(1).strip()
        try:
            parsed = json.loads(raw_obj)
        except Exception:
            try:
                parsed = ast.literal_eval(raw_obj)
            except Exception:
                return {}

        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v) for k, v in parsed.items()}

    def _parse_technitium_dns(block: str) -> dict[str, Any] | None:
        section_match = re.search(
            r"####\s+Technitium DNS Server(.*?)(?:\n####\s+|\n#\s+|\Z)",
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not section_match:
            return None

        section = section_match.group(1)
        url_match = re.search(r"\((https?://[^)\s]+)\)", section)
        user_match = re.search(r"-\s*user:\s*`([^`]+)`", section, flags=re.IGNORECASE)
        pass_match = re.search(r"-\s*password:\s*`([^`]+)`", section, flags=re.IGNORECASE)

        result: dict[str, Any] = {
            "url": url_match.group(1) if url_match else None,
            "username": user_match.group(1) if user_match else None,
            "password": pass_match.group(1) if pass_match else None,
        }
        if not any(result.values()):
            return None
        return result

    def _extract_code_block(raw_text: str, title: str) -> str | None:
        pattern = rf"\*\*{re.escape(title)}\*\*:\s*```(?:[A-Za-z0-9_+-]+)?\s*(.*?)```"
        match = re.search(pattern, raw_text, flags=re.DOTALL)
        if not match:
            return None
        return match.group(1).strip()

    def _ordered_unique(values: list[Any]) -> list[Any]:
        ordered: list[Any] = []
        for value in values:
            if value not in ordered:
                ordered.append(value)
        return ordered

    def _extract_label_value(raw_text: str, label: str) -> str | None:
        patterns = (
            rf"\*\*{re.escape(label)}\*\*:\s*`([^`]+)`",
            rf"\*\*{re.escape(label)}\*\*:\s*([^\n`]+)",
            rf"{re.escape(label)}\s*:\s*`([^`]+)`",
            rf"{re.escape(label)}\s*:\s*([^\n`]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, raw_text, flags=re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                return value or None
        return None

    def _maybe_int(value: str | None) -> int | str | None:
        if value is None:
            return None
        stripped = value.strip()
        if re.fullmatch(r"-?\d+", stripped):
            try:
                return int(stripped)
            except ValueError:
                return stripped
        return stripped

    def _component_label(raw_text: str, fallback: str) -> str:
        match = re.search(r"The component\s+`([^`]+)`\s+has been", raw_text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return fallback.strip()

    def _component_ips(interfaces: dict[str, str]) -> list[str]:
        return _ordered_unique(
            [str(value).strip() for value in interfaces.values() if str(value).strip()]
        )

    def _component_ports(raw_text: str) -> list[int]:
        ports_found = re.findall(r"available on port\s+(\d+)", raw_text, flags=re.IGNORECASE)
        return _ordered_unique([int(port) for port in ports_found])

    def _prune_none_values(data: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in data.items()
            if value is not None and value != [] and value != {}
        }

    def _build_component_entry(component_name: str, block: str) -> dict[str, Any]:
        interfaces = _parse_interfaces(block)
        ips = _component_ips(interfaces)
        ports = _component_ports(block)
        usernames = re.findall(
            r"\*\*(?:Username|user)\*\*:\s*`([^`]+)`", block, flags=re.IGNORECASE
        )
        passwords = re.findall(
            r"\*\*(?:Password|password)\*\*:\s*`([^`]+)`", block, flags=re.IGNORECASE
        )

        credentials = _prune_none_values(
            {
                "username": usernames[0].strip() if usernames else None,
                "password": passwords[0].strip() if passwords else None,
                "usernames": _ordered_unique(
                    [username.strip() for username in usernames if username.strip()]
                ),
                "passwords": _ordered_unique(
                    [password.strip() for password in passwords if password.strip()]
                ),
                "organization": _extract_label_value(block, "Organization"),
                "bucket": _extract_label_value(block, "Bucket"),
                "token": _extract_label_value(block, "Token"),
            }
        )

        technitium_dns = _parse_technitium_dns(block)
        extra_info = _prune_none_values(
            {
                "opennebula_vm_id": _maybe_int(_extract_label_value(block, "OpenNebula VM ID")),
                "opennebula_vnet_id": _maybe_int(_extract_label_value(block, "OpenNebula VNet ID")),
                "vm_memory_mib": _maybe_int(_extract_label_value(block, "VM memory")),
                "vm_vcpus": _maybe_int(_extract_label_value(block, "VM VCPUs")),
                "vm_available_storage_gib": _maybe_int(
                    _extract_label_value(block, "VM available storage")
                ),
                "vxlan_subnet": _extract_label_value(block, "VXLAN subnet"),
                "vxlan_first_ip": _extract_label_value(block, "VXLAN first IP"),
                "vxlan_address_size": _maybe_int(_extract_label_value(block, "VXLAN address size")),
                "vxlan_netmask": _maybe_int(_extract_label_value(block, "VXLAN netmask")),
                "vxlan_gateway": _extract_label_value(block, "VXLAN gateway"),
                "vxlan_dns": _extract_label_value(block, "VXLAN DNS"),
                "vxlan_mtu": _maybe_int(_extract_label_value(block, "VXLAN MTU")),
                "vxlan_guest_mtu": _maybe_int(_extract_label_value(block, "VXLAN guest MTU")),
                "network_interfaces": interfaces or None,
                "technitium_dns": technitium_dns,
            }
        )

        return {
            "name": _component_label(block, component_name),
            "ip": ips[0] if ips else None,
            "ips": ips,
            "port": ports[0] if ports else None,
            "ports": ports,
            "credentials": credentials or None,
            "extra_info": extra_info or None,
        }

    def _component_category(component_name: str, block: str) -> str:
        component_name_lower = component_name.lower()
        block_lower = block.lower()
        # El nombre del componente es la señal más fiable de su categoría. Si
        # identifica sin ambigüedad un componente elcm o monitoring, prevalece
        # sobre los tokens del bloque, evitando falsos positivos por contenido.
        if "elcm" in component_name_lower:
            return "elcm"
        if any(
            token in component_name_lower
            for token in ("monitoring", "influxdb", "grafana", "prometheus")
        ):
            return "monitoring"
        # tn_init agrupa la infraestructura base de la TN (vxlan y bastion). El
        # token "vxlan" se comprueba SOLO contra el nombre: en el bloque aparece
        # también como "VXLAN subnet" dentro de un componente vnet, y matchearlo
        # por contenido lo clasificaría erróneamente como tn_init (descartándolo).
        # Las credenciales del bastion sí se detectan por contenido del bloque.
        if "vxlan" in component_name_lower or any(
            token in component_name_lower or token in block_lower
            for token in ("wireguard vpn client config", "private key", "technitium dns")
        ):
            return "tn_init"
        if any(
            token in component_name_lower or token in block_lower
            for token in ("monitoring", "influxdb", "grafana", "prometheus")
        ):
            return "monitoring"
        if any(
            token in component_name_lower or token in block_lower
            for token in ("elcm", "backend dashboard", "frontend dashboard")
        ):
            return "elcm"
        return "auxiliary"

    def _tn_init_subkey(component_name: str, block: str) -> str:
        hint = f"{component_name} {block}".lower()
        if "vxlan" in hint:
            return "tn_vxlan"
        if "bastion" in hint:
            return "tn_bastion"
        return "tn_init"

    raw_text = report_markdown or ""
    if not raw_text:
        return {
            "private_ssh_key": None,
            "wireguard_client_config": None,
            "tn_vxlan": None,
            "tn_bastion": None,
            "technitium_dns": None,
            "monitoring": None,
            "elcm": None,
            "components": {},
            "components_count": 0,
        }

    component_blocks = _extract_component_blocks(raw_text)
    result: dict[str, Any] = {
        "private_ssh_key": None,
        "wireguard_client_config": None,
        "tn_vxlan": None,
        "tn_bastion": None,
        "technitium_dns": None,
        "monitoring": None,
        "elcm": None,
        "components": {},
        "components_count": len(component_blocks),
    }

    for component_name, block in component_blocks.items():
        component_data = _build_component_entry(component_name, block)
        category = _component_category(component_name, block)

        if category == "tn_init":
            subkey = _tn_init_subkey(component_name, block)
            if subkey == "tn_vxlan" and result["tn_vxlan"] is None:
                result["tn_vxlan"] = component_data
            elif subkey == "tn_bastion" and result["tn_bastion"] is None:
                result["tn_bastion"] = component_data

            private_key = _extract_code_block(block, "Private key")
            if private_key and not result["private_ssh_key"]:
                result["private_ssh_key"] = private_key

            wg_match = re.search(
                r"\*\*wg_client\d+\*\*:\s*```(?:[A-Za-z0-9_+-]+)?\s*(.*?)```",
                block,
                flags=re.DOTALL,
            )
            wireguard_client_config = wg_match.group(1).strip() if wg_match else None
            if wireguard_client_config and not result["wireguard_client_config"]:
                result["wireguard_client_config"] = wireguard_client_config

            technitium_data = (
                component_data.get("extra_info", {}).get("technitium_dns")
                if component_data.get("extra_info")
                else None
            )
            if technitium_data and not result["technitium_dns"]:
                result["technitium_dns"] = technitium_data
        elif category == "monitoring" and result["monitoring"] is None:
            result["monitoring"] = component_data
        elif category == "elcm" and result["elcm"] is None:
            result["elcm"] = component_data
        else:
            result["components"][component_name] = component_data

    return result


def _legacy_multipart_from_infra(
    infra: InfrastructureConfig,
    descriptor_path: str | None = None,
) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]]]:
    descriptor_ref = (
        descriptor_path
        or infra.parameters.get("descriptor")
        or resolve_examples_path(infra.descriptor_path)
    )
    reference_type = infra.parameters.get("library_reference_type")
    reference_value = infra.parameters.get("library_reference_value")
    custom_tn_id = resolve_tn_id(infra)

    if not descriptor_ref:
        raise ValueError(
            "Missing descriptor: use infrastructure.descriptor_path or parameters['descriptor']"
        )
    if not reference_type or not reference_value:
        raise ValueError(
            "Missing library_reference_type/library_reference_value in infrastructure.parameters"
        )

    data: dict[str, str] = {
        "library_reference_type": str(reference_type),
        "library_reference_value": str(reference_value),
    }
    if custom_tn_id:
        data["tn_id"] = str(custom_tn_id)

    descriptor_path = Path(str(descriptor_ref))
    if not descriptor_path.exists() or not descriptor_path.is_file():
        raise ValueError(f"TN descriptor file not found: {descriptor_ref}")

    descriptor_name = descriptor_path.name
    descriptor_bytes = descriptor_path.read_bytes()
    content_type = "application/x-yaml"

    files = {
        "descriptor": (descriptor_name, descriptor_bytes, content_type),
    }
    return data, files


async def _recover_tn_with_destroy_purge(tn_id: str, execution_id: str | None = None) -> None:
    """Reusable recovery sequence used before redeploy attempts."""
    if TNLCM_RECOVERY_DESTROY_DELAY > 0:
        logger.info(
            "Waiting %ss before destroying TN %s for recovery",
            TNLCM_RECOVERY_DESTROY_DELAY,
            tn_id,
        )
        await asyncio.sleep(TNLCM_RECOVERY_DESTROY_DELAY)

    await destroy_trial_network(tn_id, execution_id=execution_id)

    if TNLCM_REDEPLOY_DELAY > 0:
        logger.info(
            "Waiting %ss after destroy/purge for TN %s before redeploy",
            TNLCM_REDEPLOY_DELAY,
            tn_id,
        )
        await asyncio.sleep(TNLCM_REDEPLOY_DELAY)


async def deploy_trial_network(
    infra: InfrastructureConfig,
    redeploy_attempt: int = 0,
    execution_id: str | None = None,
    generated_descriptor_path: str | None = None,
    on_progress: OnProgress | None = None,
) -> str:
    """Create TN and trigger activate. Returns tn_id.

    `on_progress` es opcional y recibe una linea por reintento de activate, para
    que el llamante pueda contarlo mientras ocurre en vez de dejarlo solo en el log.
    """
    async with httpx.AsyncClient(timeout=None) as client:
        create_data: dict[str, Any] | None = None
        tn_id: str | None = None
        # True si la reconciliación detecta que la TN ya está 'activated': en ese
        # caso se salta el bloque de activate y se levanta solo la VPN.
        already_activated = False

        # Medir duración de CREATE
        create_timer = telemetry.start_timer("tnlcm", "create", execution_id=execution_id)
        create_timer.start()
        telemetry.log_event(
            "info",
            "tnlcm.create.api_call.started",
            service="tnlcm",
            operation="create",
            execution_id=execution_id,
        )

        # Preferred endpoint from project steps
        try:
            form_data, form_files = _legacy_multipart_from_infra(
                infra,
                descriptor_path=generated_descriptor_path,
            )
            response = await client.post(
                f"{settings.tnlcm_url}/api/v1/trial-network/legacy",
                data=form_data,
                files=form_files,
                headers=_headers(),
                timeout=None,
            )
            log_http_response("TNLCM", response)
            response.raise_for_status()
            create_data = response.json()
        except httpx.HTTPStatusError as exc:
            log_http_response("TNLCM", exc.response)
            # Reconciliación: TNLCM devuelve 400/409 cuando la TN ya existe. En vez
            # de abortar, se consulta su estado real y se continúa por la fase que
            # reste (activar / levantar VPN). Solo si la TN no existe o está en un
            # estado terminal se propaga el error original (fail-fast).
            reconciled_state: str | None = None
            intended_tn_id: str | None = None
            if exc.response.status_code in TNLCM_ALREADY_EXISTS_STATUS_CODES:
                intended_tn_id = resolve_tn_id(infra)
                try:
                    reconciled_state = await get_tn_state(intended_tn_id, client)
                except httpx.HTTPError as state_exc:
                    logger.warning(
                        "TNLCM reconcile status check failed for tn_id=%s: %s",
                        intended_tn_id,
                        state_exc,
                    )
                    reconciled_state = None

            if reconciled_state in TN_STATE_ACTIVATED or reconciled_state in TN_STATE_CREATED:
                logger.info(
                    "TNLCM create returned %s but tn_id=%s is already '%s'; "
                    "reconciling forward instead of failing.",
                    exc.response.status_code,
                    intended_tn_id,
                    reconciled_state,
                )
                telemetry.log_event(
                    "info",
                    "tnlcm.create.reconciled",
                    service="tnlcm",
                    operation="create",
                    execution_id=execution_id,
                    tn_id=intended_tn_id,
                    state=reconciled_state,
                )
                tn_id = intended_tn_id
                already_activated = reconciled_state in TN_STATE_ACTIVATED
                # La TN ya está registrada: se salta la espera de 20s posterior al
                # create (create_data=None evita el sleep) y, si está 'activated',
                # también el activate.
                create_data = None
            else:
                create_timer.stop(status="error")
                telemetry.log_event(
                    "error",
                    "tnlcm.create.api_call.failed",
                    service="tnlcm",
                    operation="create",
                    execution_id=execution_id,
                )
                telemetry.increment_counter(
                    "errors_total", labels={"service": "tnlcm", "operation": "create"}
                )
                if reconciled_state in TN_STATE_TERMINAL:
                    raise RuntimeError(
                        f"TNLCM tn_id={intended_tn_id} already exists in terminal state "
                        f"'{reconciled_state}'. Delete it (DELETE /executions/{{id}}/tn) "
                        "before redeploying."
                    )
                _raise_legacy_create_error(exc.response)

        create_timer.stop(status="success")
        telemetry.log_event(
            "info",
            "tnlcm.create.api_call.completed",
            service="tnlcm",
            operation="create",
            execution_id=execution_id,
        )
        telemetry.increment_counter("tnlcm_create_api_total", labels={"service": "tnlcm"})

        if tn_id is None:
            tn_id = _extract_tn_id(create_data or {})

        if not tn_id:
            raise ValueError(f"TNLCM did not return a valid tn_id: {create_data}")

        # TNLCM necesita una pequeña ventana para registrar la TN antes de activar.
        # Skip if TN was already activated
        if create_data is not None:
            await asyncio.sleep(20)

        activate_payload: dict[str, Any] = ActivateRequest(
            tn_id=tn_id,
            jenkins_deploy_pipeline=infra.parameters.get("jenkins_deploy_pipeline") or None,
        ).model_dump(exclude_none=True)

        # Si la reconciliación detectó la TN ya 'activated', se salta el activate.
        if not already_activated:
            try:
                try:
                    await _activate_with_backoff(
                        request_call=lambda: client.put(
                            f"{settings.tnlcm_url}/api/v1/trial-networks/{tn_id}/activate",
                            headers=_headers(),
                            timeout=None,
                        ),
                        tn_id=tn_id,
                        endpoint_label="new",
                        execution_id=execution_id,
                        on_progress=on_progress,
                    )
                except httpx.HTTPStatusError as exc:
                    log_http_response("TNLCM", exc.response)
                    # Compatibilidad con despliegues legacy que esperan body con tn_id.
                    if exc.response.status_code in {404, 405}:
                        await _activate_with_backoff(
                            request_call=lambda: client.post(
                                f"{settings.tnlcm_url}/api/v1/trial-network/activate",
                                json=activate_payload,
                                headers=_json_headers(),
                                timeout=None,
                            ),
                            tn_id=tn_id,
                            endpoint_label="legacy",
                            execution_id=execution_id,
                            on_progress=on_progress,
                        )
                    elif exc.response.status_code == 400:
                        # 400 al activar puede significar que la TN ya está activada:
                        # se confirma con el estado real antes de decidir.
                        state = await get_tn_state(tn_id, client)
                        if state in TN_STATE_ACTIVATED:
                            logger.info(
                                "TNLCM activate returned 400 but tn_id=%s is already "
                                "'activated'; continuing.",
                                tn_id,
                            )
                        else:
                            raise
                    elif exc.response.status_code in {409, 422}:
                        logger.warning(
                            "TNLCM activate returned %s for tn_id=%s; continuing.",
                            exc.response.status_code,
                            tn_id,
                        )
                    else:
                        raise
            except (_ActivateNoSuchFileError, _ActivateRetryExhaustedError) as activate_error:
                if redeploy_attempt >= TNLCM_ACTIVATE_REDEPLOY_MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"TNLCM activate recovery exhausted for tn_id={tn_id}: {activate_error}"
                    )

                logger.warning(
                    "TNLCM activate recovery for tn_id=%s. Destroying/purging and redeploying (attempt %s/%s). Cause: %s",
                    tn_id,
                    redeploy_attempt + 1,
                    TNLCM_ACTIVATE_REDEPLOY_MAX_ATTEMPTS,
                    activate_error,
                )
                await _recover_tn_with_destroy_purge(tn_id, execution_id=execution_id)
                return await deploy_trial_network(
                    infra,
                    redeploy_attempt=redeploy_attempt + 1,
                    execution_id=execution_id,
                    generated_descriptor_path=generated_descriptor_path,
                    on_progress=on_progress,
                )

        telemetry.log_event(
            "info",
            "tnlcm.deploy.completed",
            service="tnlcm",
            operation="deploy",
            execution_id=execution_id,
            tn_id=tn_id,
        )
        logger.info(f"TN created with id: {tn_id}")
        return tn_id


def download_trial_network_report(tn_id: str, execution_id: str | None = None) -> str:
    """Download TNLCM deployment report synchronously and return raw markdown.

    `execution_id` es opcional para no romper llamadas existentes, pero el
    orquestador debe pasarlo: sin el, la medida queda sin correlacionar y no
    aparece en el resumen de la ejecucion.
    """
    url = f"{settings.tnlcm_url}/api/v1/trial-networks/{tn_id}/report/download"
    telemetry.increment_counter(
        "requests_total", labels={"service": "tnlcm", "operation": "download_report"}
    )
    report_timer = telemetry.start_timer("tnlcm", "download_report", execution_id)
    report_timer.start()
    with httpx.Client(timeout=TNLCM_REQUEST_TIMEOUT) as client:
        try:
            response = client.get(
                url,
                headers=_headers(),
                timeout=TNLCM_REQUEST_TIMEOUT,
            )
            log_http_response("TNLCM", response)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log_http_response("TNLCM", exc.response)
            status_code = exc.response.status_code
            if status_code == 404:
                detail = response_error_detail(exc.response)
                raise TnNotFoundError(
                    (
                        f"TN {tn_id} does not exist (404) while downloading report. "
                        f"Backend error: {detail or 'unknown'}"
                    )
                ) from exc
            if status_code == 400:
                detail = response_error_detail(exc.response)
                raise TnNotActivatedError(
                    (
                        f"TN {tn_id} is not activated yet (400); report is not available. "
                        f"Backend error: {detail or 'unknown'}"
                    )
                ) from exc
            if status_code == 500:
                detail = response_error_detail(exc.response)
                raise TnReportGenerationError(
                    (
                        f"TNLCM failed to generate/read report for TN {tn_id} "
                        f"(500 generation/IO error). Backend error: {detail or 'unknown'}"
                    )
                ) from exc
            detail = response_error_detail(exc.response)
            raise TnReportDownloadError(
                (
                    f"TNLCM report download failed for TN {tn_id} with HTTP {status_code}. "
                    f"Backend error: {detail or 'unknown'}"
                )
            ) from exc
        except httpx.TimeoutException as exc:
            raise TnReportDownloadError(f"Timeout downloading TNLCM report for TN {tn_id}") from exc
        except httpx.TransportError as exc:
            raise TnReportDownloadError(
                f"Transport error downloading TNLCM report for TN {tn_id}: {exc}"
            ) from exc

    report_markdown = _extract_report_markdown(response)
    logger.info(
        "TNLCM report ready for tn_id=%s (%s bytes)", tn_id, len(report_markdown.encode("utf-8"))
    )
    try:
        report_timer.stop(status="success")
    except Exception:
        pass
    return report_markdown


def get_tn_status(tn_id: str) -> str:
    """Get Trial Network status synchronously (READY, FAILED, DEPLOYING, etc.)."""
    url = f"{settings.tnlcm_url}/api/v1/trial-networks/{tn_id}"

    with httpx.Client(timeout=TNLCM_REQUEST_TIMEOUT) as client:
        try:
            response = client.get(
                url,
                headers=_headers(),
                timeout=TNLCM_REQUEST_TIMEOUT,
            )
            log_http_response("TNLCM", response)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log_http_response("TNLCM", exc.response)
            if exc.response.status_code == 404:
                detail = response_error_detail(exc.response)
                raise TnStatusBadRequestError(
                    (
                        f"TN {tn_id} not found (mapped to 400 client error). "
                        f"Backend error: {detail or 'unknown'}"
                    )
                ) from exc
            raise

    data = response.json()
    status = data.get("status") or data.get("state") or "UNKNOWN"
    logger.debug(f"TN {tn_id} status: {status}")
    return status


def _normalize_tn_state(raw: Any) -> str | None:
    """Normaliza el estado devuelto por TNLCM a minúsculas sin espacios."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


async def get_tn_state(tn_id: str, client: httpx.AsyncClient | None = None) -> str | None:
    """Consulta el estado real de una TN en TNLCM (campo `state`), sin bloquear.

    Devuelve el estado normalizado (minúsculas) o None si la TN no existe (404).
    Reutiliza el `client` async abierto si se pasa; en caso contrario abre uno
    propio con timeout explícito (§8.1). Solo se usa el campo `state`/`status`;
    nunca se persiste el `raw_descriptor` ni secretos del payload (§8.7).
    """
    url = f"{settings.tnlcm_url}/api/v1/trial-networks/{tn_id}"

    async def _probe(active_client: httpx.AsyncClient) -> str | None:
        try:
            response = await active_client.get(
                url,
                headers=_headers(),
                timeout=TNLCM_REQUEST_TIMEOUT,
            )
            log_http_response("TNLCM", response)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log_http_response("TNLCM", exc.response)
            if exc.response.status_code == 404:
                return None
            raise
        data = response.json()
        return _normalize_tn_state(data.get("state") or data.get("status"))

    if client is not None:
        return await _probe(client)
    async with httpx.AsyncClient(timeout=TNLCM_REQUEST_TIMEOUT) as own_client:
        return await _probe(own_client)


async def destroy_trial_network(tn_id: str, execution_id: str | None = None) -> None:
    """Destroy and purge TN using DELETE endpoints.

    `execution_id` es opcional por compatibilidad, pero el orquestador lo pasa
    siempre para que los tiempos de borrado y purgado caigan en el resumen de la
    ejecucion en lugar de quedar sueltos.
    """
    telemetry.increment_counter(
        "requests_total", labels={"service": "tnlcm", "operation": "destroy"}
    )
    destroy_timer = telemetry.start_timer("tnlcm", "destroy", execution_id)
    destroy_timer.start()
    async with httpx.AsyncClient(timeout=None) as client:
        # First destroy
        destroy_ok = False
        try:
            response = await client.delete(
                f"{settings.tnlcm_url}/api/v1/trial-networks/{tn_id}/destroy",
                headers=_headers(),
                timeout=None,
            )
            log_http_response("TNLCM", response)
            response.raise_for_status()
            logger.info(f"TN {tn_id} destroyed successfully")
            destroy_ok = True
        except httpx.HTTPStatusError as exc:
            log_http_response("TNLCM", exc.response)
            if exc.response.status_code != 404:
                logger.warning(f"Failed to destroy TN {tn_id}: {exc}")

        try:
            destroy_timer.stop(status="success" if destroy_ok else "error")
        except Exception:
            pass

        # Then purge
        purged_timer = telemetry.start_timer("tnlcm", "purged", execution_id)
        purged_timer.start()
        purge_ok = False
        try:
            response = await client.delete(
                f"{settings.tnlcm_url}/api/v1/trial-networks/{tn_id}/purge",
                headers=_headers(),
                timeout=None,
            )
            log_http_response("TNLCM", response)
            response.raise_for_status()
            logger.info(f"TN {tn_id} purged successfully")
            purge_ok = True
        except httpx.HTTPStatusError as exc:
            log_http_response("TNLCM", exc.response)
            if exc.response.status_code != 404:
                logger.warning(f"Failed to purge TN {tn_id}: {exc}")
        try:
            purged_timer.stop(status="success" if purge_ok else "error")
        except Exception:
            pass


def extract_elcm_url_from_report(report_summary: dict[str, Any]) -> str | None:
    """
    Extract ELCM backend URL from trial network report summary.

    Looks first at the fixed `elcm` field, then falls back to
    auxiliary or legacy component collections for backwards compatibility.

    Returns: "http://ip:port" or None if it cannot be resolved
    """
    if not report_summary or not isinstance(report_summary, dict):
        return None

    def _first_ip(component_data: Any) -> str | None:
        if not isinstance(component_data, dict):
            return None
        ip = component_data.get("ip")
        if isinstance(ip, str) and ip.strip():
            return ip.strip()
        ips = component_data.get("ips")
        if isinstance(ips, list):
            for candidate in ips:
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        return None

    # Check fixed elcm field
    elcm_data = report_summary.get("elcm")
    ip = _first_ip(elcm_data)
    if ip:
        url = f"http://{ip}:5001"
        logger.info(f"Extracted ELCM URL from report: {url}")
        return url

    # Check auxiliary components
    components = report_summary.get("components", {})
    if isinstance(components, dict):
        for component_name, component_data in components.items():
            if "elcm" in str(component_name).lower():
                ip = _first_ip(component_data)
                if ip:
                    url = f"http://{ip}:5001"
                    logger.info(f"Extracted ELCM URL from report: {url}")
                    return url

    return None
