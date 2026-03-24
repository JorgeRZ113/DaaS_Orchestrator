import json
import logging
import asyncio
import ast
import re
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.config import settings
from app.models import InfrastructureConfig

logger = logging.getLogger(__name__)


class _ActivateNoSuchFileError(Exception):
    """Raised when TNLCM activate fails due to missing file on backend side."""


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


def _headers() -> dict[str, str]:
    token = settings.tnlcm_token.strip()
    if not token:
        raise ValueError("TNLCM_TOKEN is empty")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _json_headers() -> dict[str, str]:
    headers = _headers()
    headers["Content-Type"] = "application/json"
    return headers


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


def _load_descriptor_value(descriptor: Any) -> Any:
    if not isinstance(descriptor, str):
        return descriptor

    resolved = _resolve_examples_path(descriptor)
    if not resolved:
        return descriptor

    path = Path(resolved)
    if not path.exists() or not path.is_file():
        return descriptor

    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(text)
    if suffix in {".yml", ".yaml"}:
        return yaml.safe_load(text)
    return text


def _extract_tn_id(data: dict[str, Any]) -> str | None:
    return (
        data.get("tn_id")
        or data.get("id")
        or data.get("trial_network_id")
        or (data.get("data") or {}).get("tn_id")
    )


def _safe_json_response(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text


def summarize_trial_network_report(tn_id: str, report_payload: Any) -> dict[str, Any]:
    """Extract key fields from TNLCM RAW report text and keep a generic fallback."""

    def _extract_raw_text(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            raw = payload.get("report")
            if isinstance(raw, str):
                return raw
        return ""

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

    raw_text = _extract_raw_text(report_payload)
    if not raw_text:
        return {
            "tn_id": tn_id,
            "items_found": 0,
            "highlights": [],
        }

    component_blocks = _extract_component_blocks(raw_text)
    components: dict[str, Any] = {}
    vnet_index: list[dict[str, Any]] = []
    technitium_dns: dict[str, Any] | None = None

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
            "opennebula_vnet_id": opennebula_vnet_id,
            "network_interfaces": interfaces,
            "ips": ips,
            "ports": ports,
            "usernames": usernames,
            "passwords": passwords,
        }

        technitium_data = _parse_technitium_dns(block)
        if technitium_data:
            comp_data["technitium_dns"] = technitium_data
            if technitium_dns is None:
                technitium_dns = technitium_data

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
        "tn_id": tn_id,
        "private_ssh_key": private_key,
        "wireguard_client_config": wireguard_client_config,
        "technitium_dns": technitium_dns,
        "opennebula_vnet_index": vnet_index,
        "components": components,
        "components_count": len(components),
    }


def _legacy_payload_from_infra(infra: InfrastructureConfig) -> dict[str, Any]:
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

    payload: dict[str, Any] = {
        "descriptor": _load_descriptor_value(descriptor_ref),
        "library_reference_type": reference_type,
        "library_reference_value": reference_value,
    }
    if custom_tn_id:
        payload["tn_id"] = custom_tn_id
    return payload


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
    if descriptor_path.exists() and descriptor_path.is_file():
        suffix = descriptor_path.suffix.lower()
        if suffix == ".json":
            content_type = "application/json"
        elif suffix in {".yml", ".yaml"}:
            content_type = "application/x-yaml"
        else:
            content_type = "text/plain"
        descriptor_name = descriptor_path.name
        descriptor_bytes = descriptor_path.read_bytes()
    else:
        descriptor_value = _load_descriptor_value(descriptor_ref)
        if isinstance(descriptor_value, (dict, list)):
            descriptor_name = "descriptor.json"
            descriptor_bytes = json.dumps(descriptor_value).encode("utf-8")
            content_type = "application/json"
        else:
            descriptor_name = "descriptor.txt"
            descriptor_bytes = str(descriptor_value).encode("utf-8")
            content_type = "text/plain"

    files = {
        "descriptor": (descriptor_name, descriptor_bytes, content_type),
    }
    return data, files


async def deploy_trial_network(
    infra: InfrastructureConfig,
    redeploy_attempt: int = 0,
) -> str:
    """Create TN and trigger activate. Returns tn_id."""
    async with httpx.AsyncClient(timeout=None) as client:
        create_data: dict[str, Any] | None = None

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
            # Backward compatible fallback with initial template endpoint
            if exc.response.status_code != 404:
                raise
            fallback_payload = {
                "name": infra.name,
                "descriptor": _resolve_examples_path(infra.descriptor_path),
                "parameters": infra.parameters,
            }
            response = await client.post(
                f"{settings.tnlcm_url}/api/v1/trial-networks",
                json=fallback_payload,
                headers=_json_headers(),
                timeout=None,
            )
            _log_http_response("TNLCM", response)
            response.raise_for_status()
            create_data = response.json()

        tn_id = _extract_tn_id(create_data or {})
        if not tn_id:
            raise ValueError(f"TNLCM did not return a valid tn_id: {create_data}")

        # TNLCM necesita una pequeña ventana para registrar la TN antes de activar.
        await asyncio.sleep(20)

        activate_payload: dict[str, Any] = {"tn_id": tn_id}
        jenkins_pipeline = infra.parameters.get("jenkins_deploy_pipeline")
        if jenkins_pipeline:
            activate_payload["jenkins_deploy_pipeline"] = jenkins_pipeline

        try:
            try:
                for attempt in range(2):
                    response = await client.put(
                        f"{settings.tnlcm_url}/api/v1/trial-networks/{tn_id}/activate",
                        headers=_headers(),
                        timeout=None,
                    )
                    _log_http_response("TNLCM", response)
                    try:
                        response.raise_for_status()
                        break
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 500 and attempt == 0:
                            logger.warning(
                                "TNLCM activate returned 500 for tn_id=%s; retrying in %s seconds.",
                                tn_id,
                                settings.tnlcm_activate_retry_delay,
                            )
                            await asyncio.sleep(settings.tnlcm_activate_retry_delay)
                            continue
                        if _is_no_such_file_error(exc.response):
                            raise _ActivateNoSuchFileError() from exc
                        raise
            except httpx.HTTPStatusError as exc:
                _log_http_response("TNLCM", exc.response)
                # Compatibilidad con despliegues legacy que esperan body con tn_id.
                if exc.response.status_code in {404, 405}:
                    for attempt in range(2):
                        response = await client.post(
                            f"{settings.tnlcm_url}/api/v1/trial-network/activate",
                            json=activate_payload,
                            headers=_json_headers(),
                            timeout=None,
                        )
                        _log_http_response("TNLCM", response)
                        try:
                            response.raise_for_status()
                            break
                        except httpx.HTTPStatusError as legacy_exc:
                            if legacy_exc.response.status_code == 500 and attempt == 0:
                                logger.warning(
                                    "TNLCM legacy activate returned 500 for tn_id=%s; retrying in %s seconds.",
                                    tn_id,
                                    settings.tnlcm_activate_retry_delay,
                                )
                                await asyncio.sleep(settings.tnlcm_activate_retry_delay)
                                continue
                            if _is_no_such_file_error(legacy_exc.response):
                                raise _ActivateNoSuchFileError() from legacy_exc
                            raise
                elif exc.response.status_code in {409, 422}:
                    logger.warning(
                        "TNLCM activate returned %s for tn_id=%s; continuing.",
                        exc.response.status_code,
                        tn_id,
                    )
                else:
                    raise
        except _ActivateNoSuchFileError:
            if redeploy_attempt >= settings.tnlcm_activate_redeploy_max_attempts:
                raise RuntimeError(
                    "TNLCM activate returned 'No such file or directory' and max redeploy attempts reached"
                )

            logger.warning(
                "TNLCM activate for tn_id=%s returned missing-file error. Destroying/purging and redeploying (attempt %s/%s).",
                tn_id,
                redeploy_attempt + 1,
                settings.tnlcm_activate_redeploy_max_attempts,
            )
            if settings.tnlcm_recovery_destroy_delay > 0:
                logger.info(f"Waiting {settings.tnlcm_recovery_destroy_delay}s before destroying TN {tn_id} for recovery")
                await asyncio.sleep(settings.tnlcm_recovery_destroy_delay)
            await destroy_trial_network(tn_id)
            if settings.tnlcm_redeploy_delay > 0:
                await asyncio.sleep(settings.tnlcm_redeploy_delay)
            return await deploy_trial_network(infra, redeploy_attempt=redeploy_attempt + 1)

        logger.info(f"TN created with id: {tn_id}")
        return tn_id


async def download_trial_network_report(tn_id: str) -> dict[str, Any]:
    """Download TNLCM deployment report after activation."""
    async with httpx.AsyncClient(timeout=None) as client:
        attempts = [
            (
                "GET",
                f"{settings.tnlcm_url}/api/v1/trial-networks/{tn_id}/report/download",
                {},
            ),
            (
                "GET",
                f"{settings.tnlcm_url}/api/v1/trial-network/report/download",
                {"params": {"tn_id": tn_id}},
            ),
            (
                "POST",
                f"{settings.tnlcm_url}/api/v1/trial-network/report/download",
                {"json": {"tn_id": tn_id}},
            ),
            (
                "GET",
                f"{settings.tnlcm_url}/api/v1/trial-network/{tn_id}/report/download",
                {},
            ),
        ]

        for method, url, extra in attempts:
            try:
                if method == "GET":
                    response = await client.get(
                        url,
                        headers=_headers(),
                        timeout=None,
                        **extra,
                    )
                else:
                    response = await client.post(
                        url,
                        headers=_json_headers(),
                        timeout=None,
                        **extra,
                    )

                _log_http_response("TNLCM", response)
                response.raise_for_status()
                return {
                    "tn_id": tn_id,
                    "content_type": response.headers.get("content-type", ""),
                    "report": _safe_json_response(response),
                }
            except httpx.HTTPStatusError as exc:
                _log_http_response("TNLCM", exc.response)
                if exc.response.status_code == 404:
                    continue
                raise

    raise RuntimeError(f"Could not download report for TN {tn_id}")


async def get_tn_status(tn_id: str) -> str:
    """Get Trial Network status (READY, FAILED, DEPLOYING, etc.)."""
    async with httpx.AsyncClient(timeout=None) as client:
        paths = [
            f"/api/v1/trial-network/{tn_id}",
            f"/api/v1/trial-networks/{tn_id}",
        ]

        for path in paths:
            try:
                response = await client.get(f"{settings.tnlcm_url}{path}", headers=_headers())
                _log_http_response("TNLCM", response)
                response.raise_for_status()
                data = response.json()
                status = data.get("status") or data.get("state") or "UNKNOWN"
                logger.debug(f"TN {tn_id} status: {status}")
                return status
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    continue
                raise

    return "UNKNOWN"


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
