import logging
import json
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.models import ExperimentConfig

logger = logging.getLogger(__name__)

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
    execution_id = (
        data.get("ExecutionId")  # ELCM format (uppercase)
        or data.get("experiment_id")
        or data.get("id")
        or data.get("execution_id")
        or (data.get("data") or {}).get("experiment_id")
        or (data.get("data") or {}).get("execution_id")
    )
    # Convert to string if found (ELCM may return int)
    return str(execution_id) if execution_id is not None else None


def _resolve_testcases(experiment: ExperimentConfig) -> list[str]:
    ordered: list[str] = []
    if experiment.testcase_path:
        ordered.append(experiment.testcase_path)
    ordered.extend(experiment.testcase_paths)

    # Remove duplicates preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for item in ordered:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


async def upload_test_cases(testcase_paths: list[str], user_id: int = 1) -> None:
    """Upload test cases to ELCM."""
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
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
            
            try:
                response = await client.post(
                    f"{settings.elcm_url}/elcm/api/v1/facility/upload_test_case",
                    files=files,
                    timeout=settings.request_timeout,
                )
                _log_http_response("ELCM", response)
                response.raise_for_status()
                logger.info(f"Uploaded testcase: {path.name}")
            except httpx.HTTPStatusError as exc:
                _log_http_response("ELCM", exc.response)
                logger.warning(f"Failed to upload testcase {path.name}: {exc}")


async def run_experiment(
    experiment: ExperimentConfig,
    tn_id: str,
    testcase_path_override: str | None = None,
) -> str:
    """Launch experiment in ELCM and return execution_id."""
    testcase_source = testcase_path_override or experiment.testcase_path
    resolved_testcases = []
    
    if testcase_source:
        testcase_path = _resolve_examples_path(testcase_source)
        if testcase_path:
            resolved_testcases.append(testcase_path)
    
    resolved_testcases.extend([_resolve_examples_path(p) or p for p in experiment.testcase_paths])

    # Upload test cases first (resolved paths for uploading to server)
    await upload_test_cases(resolved_testcases)

    # Load experiment descriptor from examples
    exp_descriptor_path = _resolve_examples_path("Exp_Desc.json")
    payload = {}
    
    if exp_descriptor_path:
        exp_path = Path(exp_descriptor_path)
        if exp_path.exists():
            payload = json.loads(exp_path.read_text(encoding="utf-8"))
            logger.info(f"Loaded experiment descriptor from {exp_descriptor_path}")
    
    # If no descriptor found, create a minimal payload
    if not payload:
        payload = {
            "Version": "2.1.0",
            "UserId": 1,
            "ExperimentType": "Standard",
            "TestCases": ["Test_ping"],  # Default test case name
            "UEs": [],
            "Slice": None,
            "NSs": [],
            "ExclusiveExecution": False,
            "Scenario": [],
            "Automated": True,
            "ReservationTime": None,
            "Application": experiment.name,
            "Parameters": {},
            "Remote": None,
            "RemoteDescriptor": None,
            "Extra": {},
        }
    else:
        # Update Application field with experiment name, keep TestCases from descriptor
        payload["Application"] = experiment.name

    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        response_data: dict[str, Any] | None = None
        paths = [
            "/elcm/api/v1/experiment/run",
            "/api/v1/experiment/run",
        ]

        for path in paths:
            try:
                response = await client.post(
                    f"{settings.elcm_url}{path}",
                    json=payload,
                    headers=_build_headers(json_body=True),
                )
                _log_http_response("ELCM", response)
                response.raise_for_status()
                response_data = response.json()
                break
            except httpx.HTTPStatusError as exc:
                _log_http_response("ELCM", exc.response)
                if exc.response.status_code == 404:
                    continue
                raise

        if response_data is None:
            raise RuntimeError("No valid ELCM endpoint found to run experiment")

        # Extract execution_id (ELCM returns different ID than experiment_id)
        execution_id = _extract_experiment_id(response_data)
        if not execution_id:
            raise ValueError(f"ELCM did not return a valid execution id: {response_data}")

        logger.info(f"ELCM execution created with id: {execution_id}")
        return execution_id


async def get_experiment_status(experiment_id: str) -> str:
    """Get execution status from ELCM."""
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        paths = [
            f"/elcm/api/v1/execution/{experiment_id}/status",
            f"/api/v1/execution/{experiment_id}/status",
        ]

        for path in paths:
            try:
                response = await client.get(
                    f"{settings.elcm_url}{path}",
                    headers=_build_headers(),
                )
                _log_http_response("ELCM", response)
                response.raise_for_status()
                data = response.json()
                # Look for status in various possible fields
                status = (
                    data.get("status")
                    or data.get("state")
                    or data.get("Coarse")
                    or data.get("execution_status")
                    or "UNKNOWN"
                )
                logger.debug(f"Execution {experiment_id} status: {status}")
                return status
            except httpx.HTTPStatusError as exc:
                _log_http_response("ELCM", exc.response)
                if exc.response.status_code == 404:
                    continue
                raise

    return "UNKNOWN"


async def collect_results(experiment_id: str) -> dict[str, Any]:
    """Collect experiment logs (current dataset mode: logs)."""
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        paths = [
            f"/elcm/api/v1/execution/{experiment_id}/logs",
            f"/api/v1/execution/{experiment_id}/logs",
            f"/api/v1/experiments/{experiment_id}/logs",
        ]

        for path in paths:
            try:
                response = await client.get(
                    f"{settings.elcm_url}{path}",
                    headers=_build_headers(),
                )
                _log_http_response("ELCM", response)
                response.raise_for_status()
                logs_data = response.json()
                logger.info(f"Logs collected for experiment {experiment_id}")
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
                if exc.response.status_code == 404:
                    continue
                raise

    raise RuntimeError(f"Could not collect logs for experiment {experiment_id}")
