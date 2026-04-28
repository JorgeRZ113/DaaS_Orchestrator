import json
import logging
import asyncio
import ast
import re
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.models import InfrastructureConfig

logger = logging.getLogger(__name__)

# TNLCM timing constants (kept local to this adapter)
TNLCM_REQUEST_TIMEOUT = 60
TNLCM_LOGIN_TIMEOUT_SECONDS = 20
TNLCM_ACTIVATE_MAX_ATTEMPTS = 2
TNLCM_ACTIVATE_RETRY_BASE_DELAY = 2
TNLCM_ACTIVATE_RETRY_INCREMENT = 2
TNLCM_ACTIVATE_REDEPLOY_MAX_ATTEMPTS = 1
TNLCM_REDEPLOY_DELAY = 5
TNLCM_RECOVERY_DESTROY_DELAY = 0
TNLCM_ACTIVATE_RETRYABLE_STATUS_CODES = {500, 502, 503, 504}
TNLCM_LEGACY_NON_RETRYABLE_STATUS_CODES = {400, 404, 422}
TNLCM_LEGACY_ERROR_HINT = "Revise lo indicado por el mensaje de error."


# Token storage in memory (populated by login endpoint)
_tnlcm_access_token: str | None = None
_tnlcm_refresh_token: str | None = None


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


def _activate_retry_delay_seconds(attempt_number: int) -> int:
    # attempt_number is 1-based and points to the failed attempt.
    return TNLCM_ACTIVATE_RETRY_BASE_DELAY + ((attempt_number - 1) * TNLCM_ACTIVATE_RETRY_INCREMENT)


async def _activate_with_backoff(
    request_call,
    tn_id: str,
    endpoint_label: str,
) -> None:
    for attempt in range(1, TNLCM_ACTIVATE_MAX_ATTEMPTS + 1):
        try:
            response = await request_call()
            _log_http_response("TNLCM", response)
            response.raise_for_status()
            return
        except httpx.HTTPStatusError as exc:
            _log_http_response("TNLCM", exc.response)
            if _is_no_such_file_error(exc.response):
                raise _ActivateNoSuchFileError() from exc

            status_code = exc.response.status_code
            retryable = status_code in TNLCM_ACTIVATE_RETRYABLE_STATUS_CODES
            if retryable and attempt < TNLCM_ACTIVATE_MAX_ATTEMPTS:
                delay_seconds = _activate_retry_delay_seconds(attempt)
                logger.warning(
                    "TNLCM %s activate failed for tn_id=%s (HTTP %s). Retry %s/%s in %ss.",
                    endpoint_label,
                    tn_id,
                    status_code,
                    attempt + 1,
                    TNLCM_ACTIVATE_MAX_ATTEMPTS,
                    delay_seconds,
                )
                await asyncio.sleep(delay_seconds)
                continue

            if retryable:
                detail = _response_error_detail(exc.response)
                raise _ActivateRetryExhaustedError(
                    (
                        f"TNLCM {endpoint_label} activate exhausted retries for tn_id={tn_id} "
                        f"(HTTP {status_code}). Backend error: {detail or 'unknown'}"
                    )
                ) from exc
            raise
        except httpx.TimeoutException as exc:
            if attempt < TNLCM_ACTIVATE_MAX_ATTEMPTS:
                delay_seconds = _activate_retry_delay_seconds(attempt)
                logger.warning(
                    "TNLCM %s activate timeout for tn_id=%s. Retry %s/%s in %ss.",
                    endpoint_label,
                    tn_id,
                    attempt + 1,
                    TNLCM_ACTIVATE_MAX_ATTEMPTS,
                    delay_seconds,
                )
                await asyncio.sleep(delay_seconds)
                continue
            raise _ActivateRetryExhaustedError(
                f"TNLCM {endpoint_label} activate exhausted retries for tn_id={tn_id} due to timeout"
            ) from exc
        except httpx.TransportError as exc:
            if attempt < TNLCM_ACTIVATE_MAX_ATTEMPTS:
                delay_seconds = _activate_retry_delay_seconds(attempt)
                logger.warning(
                    "TNLCM %s activate transport error for tn_id=%s: %s. Retry %s/%s in %ss.",
                    endpoint_label,
                    tn_id,
                    exc,
                    attempt + 1,
                    TNLCM_ACTIVATE_MAX_ATTEMPTS,
                    delay_seconds,
                )
                await asyncio.sleep(delay_seconds)
                continue
            raise _ActivateRetryExhaustedError(
                f"TNLCM {endpoint_label} activate exhausted retries for tn_id={tn_id} due to transport errors"
            ) from exc

def _log_http_response(service: str, response: httpx.Response) -> None:
    body = ""
    if hasattr(response, "text"):
        text_value = getattr(response, "text")
        body = text_value if text_value is not None else ""
    elif hasattr(response, "json"):
        try:
            body = json.dumps(response.json())
        except Exception:
            body = ""
    body = body.replace("\n", " ").strip()
    if len(body) > 500:
        body = f"{body[:500]}..."
    request = getattr(response, "request", None)
    method = getattr(request, "method", "?")
    url = getattr(request, "url", "?")
    status_code = getattr(response, "status_code", "?")
    logger.info(
        "%s %s %s -> %s | %s",
        service,
        method,
        url,
        status_code,
        body,
    )


def _response_error_detail(response: httpx.Response | None) -> str:
    if response is None:
        return ""

    try:
        payload = response.json()
        if isinstance(payload, dict):
            for key in ("message", "detail", "error", "errors"):
                value = payload.get(key)
                if value is None:
                    continue
                if isinstance(value, (dict, list)):
                    return json.dumps(value)
                return str(value)
            return json.dumps(payload)
        if isinstance(payload, list):
            return json.dumps(payload)
        if isinstance(payload, str):
            return payload
    except Exception:
        pass

    return (response.text or "").strip()


def _raise_legacy_create_error(response: httpx.Response | None) -> None:
    status_code = getattr(response, "status_code", "unknown")
    detail = _response_error_detail(response) or "unknown"
    if isinstance(status_code, int) and status_code in TNLCM_LEGACY_NON_RETRYABLE_STATUS_CODES:
        raise RuntimeError(
            f"TNLCM /legacy (HTTP {status_code}): {detail}. {TNLCM_LEGACY_ERROR_HINT}"
        )
    raise RuntimeError(
        f"TNLCM /legacy failed (HTTP {status_code}): {detail}. {TNLCM_LEGACY_ERROR_HINT}"
    )


def _headers() -> dict[str, str]:
    """Build headers using in-memory token (from login) or fallback to .env token."""
    token = _tnlcm_access_token or settings.tnlcm_token.strip()
    if not token:
        raise ValueError("TNLCM_TOKEN is not set. Use /tnlcm/token/refresh to login.")
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

    paths = [
        "/api/v1/user/login"
    ]

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

    access_token = (
        response_data.get("access_token")
        or response_data.get("token")
        or (response_data.get("data") or {}).get("access_token")
    )

    refresh_token = (
        response_data.get("refresh_token")
        or (response_data.get("data") or {}).get("refresh_token")
    )

    if access_token is None:
        raise ValueError(f"TNLCM login did not return access_token: {response_data}")

    token = str(access_token).strip()
    if not token:
        raise ValueError("TNLCM returned an empty access_token")

    _tnlcm_access_token = token
    if refresh_token:
        _tnlcm_refresh_token = str(refresh_token).strip()

    logger.info("TNLCM token refreshed and stored in memory")
    return token


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


def _examples_base_dir() -> Path:
    base = Path(settings.examples_dir)
    if not base.is_absolute():
        base = Path.cwd() / base
    return base.resolve()


def _resolve_examples_path(path_or_name: str | None) -> str | None:
    if not path_or_name:
        return None

    candidate = Path(path_or_name)
    if candidate.is_absolute():
        return str(candidate)

    return str((_examples_base_dir() / candidate).resolve())


def _extract_tn_id(data: dict[str, Any]) -> str | None:
    return (
        data.get("tn_id")
    )


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
        # Split by level-1 markdown headers, ignoring anything inside fenced code blocks.
        lines = raw_text.splitlines()
        headers: list[tuple[int, str]] = []
        in_fence = False

        for idx, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue

            match = re.match(r"^#\s+(.+)$", stripped)
            if match:
                headers.append((idx, match.group(1).strip()))

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

    raw_text = report_markdown or ""
    if not raw_text:
        return {
            "private_ssh_key": None,
            "wireguard_client_config": None,
            "opennebula_vnet_index": [],
            "components": {},
            "components_count": 0,
        }

    component_blocks = _extract_component_blocks(raw_text)
    components: dict[str, Any] = {}
    vnet_index: list[dict[str, Any]] = []
    for component_name, block in component_blocks.items():
        vnet_match = re.search(r"OpenNebula VNet ID\*\*:\s*`([^`]+)`", block, flags=re.IGNORECASE)
        opennebula_vnet_id = vnet_match.group(1).strip() if vnet_match else None

        interfaces = _parse_interfaces(block)
        ips = sorted(set(interfaces.values()))
        ports_found = re.findall(r"available on port\s+(\d+)", block, flags=re.IGNORECASE)
        ports = sorted({int(p) for p in ports_found})

        usernames = re.findall(r"\*\*(?:Username|user)\*\*:\s*`([^`]+)`", block)
        passwords = re.findall(r"\*\*(?:Password|password)\*\*:\s*`([^`]+)`", block)

        comp_data: dict[str, Any] = {
            "ips": ips,
            "ports": ports,
            "usernames": usernames,
            "passwords": passwords,
        }

        technitium_data = _parse_technitium_dns(block)
        if technitium_data:
            comp_data["technitium_dns"] = technitium_data

        components[component_name] = comp_data

        if opennebula_vnet_id:
            vnet_index.append(
                {
                    "component": component_name,
                    "opennebula_vnet_id": opennebula_vnet_id,
                    "interfaces": interfaces,
                    "ports": ports,
                }
            )

    private_key = _extract_code_block(raw_text, "Private key")

    wg_match = re.search(
        r"\*\*wg_client\d+\*\*:\s*```(?:[A-Za-z0-9_+-]+)?\s*(.*?)```",
        raw_text,
        flags=re.DOTALL,
    )
    wireguard_client_config = wg_match.group(1).strip() if wg_match else None

    return {
        "private_ssh_key": private_key,
        "wireguard_client_config": wireguard_client_config,
        "opennebula_vnet_index": vnet_index,
        "components": components,
        "components_count": len(components),
    }



def _legacy_multipart_from_infra(
    infra: InfrastructureConfig,
) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]]]:
    descriptor_ref = infra.parameters.get("descriptor") or _resolve_examples_path(infra.descriptor_path)
    reference_type = infra.parameters.get("library_reference_type")
    reference_value = infra.parameters.get("library_reference_value")
    custom_tn_id = infra.parameters.get("tn_id") or infra.name

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


async def _recover_tn_with_destroy_purge(tn_id: str) -> None:
    """Reusable recovery sequence used before redeploy attempts."""
    if TNLCM_RECOVERY_DESTROY_DELAY > 0:
        logger.info(
            "Waiting %ss before destroying TN %s for recovery",
            TNLCM_RECOVERY_DESTROY_DELAY,
            tn_id,
        )
        await asyncio.sleep(TNLCM_RECOVERY_DESTROY_DELAY)

    await destroy_trial_network(tn_id)

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
) -> str:
    """Create TN and trigger activate. Returns tn_id."""
    async with httpx.AsyncClient(timeout=None) as client:
        create_data: dict[str, Any] | None = None
        tn_id: str | None = None

        # Preferred endpoint from project steps
        try:
            form_data, form_files = _legacy_multipart_from_infra(infra)
            response = await client.post(
                f"{settings.tnlcm_url}/api/v1/trial-network/legacy",
                data=form_data,
                files=form_files,
                headers=_headers(),
                timeout=None,
            )
            _log_http_response("TNLCM", response)
            response.raise_for_status()
            create_data = response.json()
        except httpx.HTTPStatusError as exc:
            _log_http_response("TNLCM", exc.response)
            _raise_legacy_create_error(exc.response)

        if tn_id is None:
            tn_id = _extract_tn_id(create_data or {})
        
        if not tn_id:
            raise ValueError(f"TNLCM did not return a valid tn_id: {create_data}")

        # TNLCM necesita una pequeña ventana para registrar la TN antes de activar.
        # Skip if TN was already activated
        if create_data is not None:
            await asyncio.sleep(20)

        activate_payload: dict[str, Any] = {"tn_id": tn_id}
        jenkins_pipeline = infra.parameters.get("jenkins_deploy_pipeline")
        if jenkins_pipeline:
            activate_payload["jenkins_deploy_pipeline"] = jenkins_pipeline

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
                )
            except httpx.HTTPStatusError as exc:
                _log_http_response("TNLCM", exc.response)
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
                    )
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
            await _recover_tn_with_destroy_purge(tn_id)
            return await deploy_trial_network(infra, redeploy_attempt=redeploy_attempt + 1)

        logger.info(f"TN created with id: {tn_id}")
        return tn_id


def download_trial_network_report(tn_id: str) -> str:
    """Download TNLCM deployment report synchronously and return raw markdown."""
    url = f"{settings.tnlcm_url}/api/v1/trial-networks/{tn_id}/report/download"

    with httpx.Client(timeout=TNLCM_REQUEST_TIMEOUT) as client:
        try:
            response = client.get(
                url,
                headers=_headers(),
                timeout=TNLCM_REQUEST_TIMEOUT,
            )
            _log_http_response("TNLCM", response)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _log_http_response("TNLCM", exc.response)
            status_code = exc.response.status_code
            if status_code == 404:
                detail = _response_error_detail(exc.response)
                raise TnNotFoundError(
                    (
                        f"TN {tn_id} does not exist (404) while downloading report. "
                        f"Backend error: {detail or 'unknown'}"
                    )
                ) from exc
            if status_code == 400:
                detail = _response_error_detail(exc.response)
                raise TnNotActivatedError(
                    (
                        f"TN {tn_id} is not activated yet (400); report is not available. "
                        f"Backend error: {detail or 'unknown'}"
                    )
                ) from exc
            if status_code == 500:
                detail = _response_error_detail(exc.response)
                raise TnReportGenerationError(
                    (
                        f"TNLCM failed to generate/read report for TN {tn_id} "
                        f"(500 generation/IO error). Backend error: {detail or 'unknown'}"
                    )
                ) from exc
            detail = _response_error_detail(exc.response)
            raise TnReportDownloadError(
                (
                    f"TNLCM report download failed for TN {tn_id} with HTTP {status_code}. "
                    f"Backend error: {detail or 'unknown'}"
                )
            ) from exc
        except httpx.TimeoutException as exc:
            raise TnReportDownloadError(
                f"Timeout downloading TNLCM report for TN {tn_id}"
            ) from exc
        except httpx.TransportError as exc:
            raise TnReportDownloadError(
                f"Transport error downloading TNLCM report for TN {tn_id}: {exc}"
            ) from exc

    report_markdown = _extract_report_markdown(response)
    logger.info("TNLCM report ready for tn_id=%s (%s bytes)", tn_id, len(report_markdown.encode("utf-8")))
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
            _log_http_response("TNLCM", response)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _log_http_response("TNLCM", exc.response)
            if exc.response.status_code == 404:
                detail = _response_error_detail(exc.response)
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


async def destroy_trial_network(tn_id: str) -> None:
    """Destroy and purge TN using DELETE endpoints."""
    async with httpx.AsyncClient(timeout=None) as client:
        # First destroy
        try:
            response = await client.delete(
                f"{settings.tnlcm_url}/api/v1/trial-networks/{tn_id}/destroy",
                headers=_headers(),
                timeout=None,
            )
            _log_http_response("TNLCM", response)
            response.raise_for_status()
            logger.info(f"TN {tn_id} destroyed successfully")
        except httpx.HTTPStatusError as exc:
            _log_http_response("TNLCM", exc.response)
            if exc.response.status_code != 404:
                logger.warning(f"Failed to destroy TN {tn_id}: {exc}")

        # Then purge
        try:
            response = await client.delete(
                f"{settings.tnlcm_url}/api/v1/trial-networks/{tn_id}/purge",
                headers=_headers(),
                timeout=None,
            )
            _log_http_response("TNLCM", response)
            response.raise_for_status()
            logger.info(f"TN {tn_id} purged successfully")
        except httpx.HTTPStatusError as exc:
            _log_http_response("TNLCM", exc.response)
            if exc.response.status_code != 404:
                logger.warning(f"Failed to purge TN {tn_id}: {exc}")


def extract_elcm_url_from_report(report_summary: dict[str, Any]) -> str | None:
    """
    Extract ELCM backend URL from trial network report summary.
    
    Looks for ELCM component in the components dict and builds URL from IP and port.
    Falls back to settings.elcm_url if not found in report.
    
    Returns: "http://ip:port" or None if not found
    """
    if not report_summary or not isinstance(report_summary, dict):
        return None
    
    components = report_summary.get("components", {})
    if not components:
        return None
    
    # Look for ELCM component (could be "ELCM", "elcm", "ELCM Backend", etc.)
    for component_name, component_data in components.items():
        if "elcm" in component_name.lower() and "backend" in component_name.lower():
            comp_dict = component_data
            if isinstance(component_data, dict):
                # Try to extract IP and port
                ips = comp_dict.get("ips", [])
                ports = comp_dict.get("ports", [])
                
                if ips and ports:
                    ip = ips[0]  # Use first IP
                    port = ports[0]  # Use first port (backend port)
                    url = f"http://{ip}:{port}"
                    logger.info(f"Extracted ELCM URL from report: {url}")
                    return url
    
    return None

