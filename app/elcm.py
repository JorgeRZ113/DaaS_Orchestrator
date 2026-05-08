import logging
import json
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.models import ExperimentConfig
from app.utils.telemetry import telemetry

logger = logging.getLogger(__name__)

# ELCM timing constants (kept local to this adapter)
ELCM_REQUEST_TIMEOUT = 60
ELCM_RUN_NON_RETRYABLE_STATUS_CODES = {400}
ELCM_RUN_ERROR_HINT = (
    "Corrija lo indicado por el error antes de volver a ejecutar la parte de ELCM."
)

def _normalize_elcm_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def _resolve_elcm_url(elcm_base_url: str | None) -> str:
    """Resolve ELCM URL explicitly from orchestration context."""
    if not elcm_base_url or not elcm_base_url.strip():
        raise RuntimeError(
            "ELCM base URL is missing. TNLCM report must include ELCM component data."
        )
    normalized = _normalize_elcm_url(elcm_base_url)
    if not normalized.startswith(("http://", "https://")):
        raise RuntimeError(f"Invalid ELCM base URL resolved from report: {normalized}")
    return normalized


def _build_headers(*, json_body: bool = False) -> dict[str, str]:
    """Build ELCM headers (ELCM backend collection is noauth)."""
    headers: dict[str, str] = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


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


def _extract_experiment_id(data: dict[str, Any]) -> str | None:
    execution_id = data.get("ExecutionId")
    # Convert to string if found (ELCM may return int).
    return str(execution_id) if execution_id is not None else None


class TnLogsNotFoundError(RuntimeError):
    """Raised when ELCM reports experiment logs as logically not found."""


class TnUploadTestCaseError(RuntimeError):
    """Raised when uploading a testcase/UE to ELCM fails definitively."""


ELCM_UPLOAD_ERROR_HINT = (
    "Corrija lo indicado por el mensaje de error antes de volver a lanzar la parte de ELCM."
)


async def upload_test_cases(
    testcase_paths: list[str],
    user_id: int = 1,
    elcm_base_url: str | None = None,
    execution_id: str | None = None,
) -> None:
    """Upload test cases to ELCM."""
    base_url = _resolve_elcm_url(elcm_base_url)
    async with httpx.AsyncClient(timeout=ELCM_REQUEST_TIMEOUT) as client:
        for testcase_path in testcase_paths:
            if not testcase_path:
                continue
            
            resolved_path = _resolve_examples_path(testcase_path)
            if not resolved_path:
                logger.warning(f"Could not resolve testcase path: {testcase_path}")
                continue
            
            path = Path(resolved_path)
            if not path.exists():
                logger.warning(f"Testcase file not found: {resolved_path}")
                continue
            
            # Read the file
            with open(path, "rb") as f:
                file_content = f.read()
            
            # Upload to ELCM
            files = {
                "test_case": (path.name, file_content),
                "file_type": (None, "testcase"),
                "user_id": (None, str(user_id)),
            }
            
            telemetry.increment_counter("requests_total", labels={"service": "elcm", "operation": "upload"})
            upload_timer = telemetry.start_timer("elcm", "upload", telemetry.ensure_execution_id(execution_id))
            upload_timer.start()
            upload_status = "success"
            try:
                response = await client.post(
                    f"{base_url}/elcm/api/v1/facility/upload_test_case",
                    files=files,
                    timeout=ELCM_REQUEST_TIMEOUT,
                )
                _log_http_response("ELCM", response)
                response.raise_for_status()
                logger.info("ELCM testcase/UE uploaded successfully: %s", path.name)
            except httpx.HTTPStatusError as exc:
                _log_http_response("ELCM", exc.response)
                detail = _response_error_detail(exc.response)
                telemetry.increment_counter("errors_total", labels={"service": "elcm", "operation": "upload", "error_type": str(exc.response.status_code)})
                upload_status = "error"
                raise TnUploadTestCaseError(
                    (
                        f"ELCM upload_test_case failed for {path.name} (HTTP {exc.response.status_code}). "
                        f"Backend error: {detail or 'unknown'}. {ELCM_UPLOAD_ERROR_HINT}"
                    )
                ) from exc
            finally:
                try:
                    upload_timer.stop(status=upload_status)
                except Exception:
                    pass


async def run_experiment(
    experiment: ExperimentConfig,
    elcm_base_url: str | None = None,
    execution_id: str | None = None,
) -> str:
    """Launch Exp_Desc.json in ELCM and return execution_id."""

    # Experiment descriptor is fixed to JSON under examples.
    exp_descriptor_path = _resolve_examples_path("Exp_Desc.json")
    if not exp_descriptor_path:
        raise ValueError("Experiment descriptor path could not be resolved")

    exp_path = Path(exp_descriptor_path)
    if not exp_path.exists() or not exp_path.is_file():
        raise FileNotFoundError(f"Experiment descriptor not found: {exp_descriptor_path}")

    payload = json.loads(exp_path.read_text(encoding="utf-8"))
    logger.info(f"Loaded experiment descriptor from {exp_descriptor_path}")
    payload["Application"] = experiment.name

    base_url = _resolve_elcm_url(elcm_base_url)
    telemetry.increment_counter("requests_total", labels={"service": "elcm", "operation": "run"})
    run_timer = telemetry.start_timer("elcm", "run", telemetry.ensure_execution_id(execution_id))
    run_timer.start()
    run_status = "success"
    async with httpx.AsyncClient(timeout=ELCM_REQUEST_TIMEOUT) as client:
        try:
            response = await client.post(
                f"{base_url}/elcm/api/v1/experiment/run",
                json=payload,
                headers=_build_headers(json_body=True),
            )
            _log_http_response("ELCM", response)
            response.raise_for_status()
            response_data = response.json()
        except httpx.HTTPStatusError as exc:
            _log_http_response("ELCM", exc.response)
            status_code = exc.response.status_code
            detail = _response_error_detail(exc.response)
            telemetry.increment_counter("errors_total", labels={"service": "elcm", "operation": "run", "error_type": str(status_code)})
            run_status = "error"
            if status_code in ELCM_RUN_NON_RETRYABLE_STATUS_CODES:
                raise RuntimeError(
                    (
                        f"ELCM /experiment/run (HTTP {status_code}): {detail or 'unknown'}. "
                        f"{ELCM_RUN_ERROR_HINT}"
                    )
                ) from exc
            raise RuntimeError(
                f"ELCM run failed (HTTP {status_code}). Backend error: {detail or 'unknown'}"
            ) from exc
        finally:
            try:
                run_timer.stop(status=run_status)
            except Exception:
                pass

        # Extract execution_id (ELCM returns different ID than experiment_id)
        execution_id = _extract_experiment_id(response_data)
        if not execution_id:
            raise ValueError(f"ELCM did not return a valid execution id: {response_data}")

        logger.info(f"ELCM execution created with id: {execution_id}")
        return execution_id


async def get_experiment_status(
    experiment_id: str,
    elcm_base_url: str | None = None,
    execution_id: str | None = None,
) -> str:
    """Get execution status from ELCM."""
    telemetry.increment_counter("requests_total", labels={"service": "elcm", "operation": "status"})
    status_timer = telemetry.start_timer("elcm", "status", execution_id=telemetry.ensure_execution_id(execution_id or experiment_id))
    status_timer.start()
    status_value = "success"
    base_url = _resolve_elcm_url(elcm_base_url)
    async with httpx.AsyncClient(timeout=ELCM_REQUEST_TIMEOUT) as client:
        try:
            response = await client.get(
                f"{base_url}/elcm/api/v1/execution/{experiment_id}/status",
                headers=_build_headers(),
            )
            _log_http_response("ELCM", response)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _log_http_response("ELCM", exc.response)
            detail = _response_error_detail(exc.response)
            telemetry.increment_counter("errors_total", labels={"service": "elcm", "operation": "status", "error_type": str(exc.response.status_code)})
            status_value = "error"
            raise RuntimeError(
                (
                    f"ELCM status request failed for execution {experiment_id} "
                    f"(HTTP {exc.response.status_code}). Backend error: {detail or 'unknown'}"
                )
            ) from exc
        finally:
            try:
                status_timer.stop(status=status_value)
            except Exception:
                pass

        data = response.json()
        status = data.get("Coarse", "UNKNOWN")
        logger.debug(f"Execution {experiment_id} status: {status}")
        return status


async def collect_results(
    experiment_id: str,
    elcm_base_url: str | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    """Collect experiment logs (current dataset mode: logs)."""
    telemetry.increment_counter("requests_total", labels={"service": "elcm", "operation": "collect"})
    collect_timer = telemetry.start_timer("elcm", "collect", execution_id=telemetry.ensure_execution_id(execution_id or experiment_id))
    collect_timer.start()
    collect_status = "success"
    base_url = _resolve_elcm_url(elcm_base_url)
    async with httpx.AsyncClient(timeout=ELCM_REQUEST_TIMEOUT) as client:
        try:
            response = await client.get(
                f"{base_url}/elcm/api/v1/execution/{experiment_id}/logs",
                headers=_build_headers(),
            )
            _log_http_response("ELCM", response)
            response.raise_for_status()
            logs_data = response.json()
            if isinstance(logs_data, dict) and (
                logs_data.get("Status") == "Not Found" or logs_data.get("status") == "Not Found"
            ):
                raise TnLogsNotFoundError(
                    (
                        f"ELCM reports execution {experiment_id} as not found in logs. "
                        "El experimento no se ha podido hacer y hay que repetirlo."
                    )
                )
            logger.info("ELCM logs/metrics extracted successfully for experiment %s", experiment_id)
            return {
                "output": "logs",
                "experiment_id": experiment_id,
                "logs": logs_data,
            }
        except httpx.HTTPStatusError as exc:
            error_msg = exc.response.text
            # If logs not ready yet (file doesn't exist), return empty logs
            if "No such file or directory" in error_msg:
                logger.info(f"Logs not ready yet for experiment {experiment_id}, returning empty logs")
                return {
                    "output": "logs",
                    "experiment_id": experiment_id,
                    "logs": {"message": "Logs not available yet"},
                    "status": "logs_pending",
                }
            _log_http_response("ELCM", exc.response)
            detail = _response_error_detail(exc.response)
            telemetry.increment_counter("errors_total", labels={"service": "elcm", "operation": "collect", "error_type": str(exc.response.status_code)})
            collect_status = "error"
            raise RuntimeError(
                (
                    f"ELCM logs request failed for execution {experiment_id} "
                    f"(HTTP {exc.response.status_code}). Backend error: {detail or 'unknown'}"
                )
            ) from exc
        finally:
            try:
                collect_timer.stop(status=collect_status)
            except Exception:
                pass
