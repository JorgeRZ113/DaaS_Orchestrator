import asyncio
import logging
import json
import re
from pathlib import Path
from typing import Any

import httpx
import yaml

from app import artifacts
from app.config import settings
from app.models import ExperimentConfig
from app.utils.telemetry import telemetry
from app.utils.ytt_renderer import resolve_template_path

logger = logging.getLogger(__name__)

# ELCM timing constants (kept local to this adapter)
ELCM_REQUEST_TIMEOUT = 60
# La descarga del ZIP de resultados puede ser mayor que una llamada JSON normal.
ELCM_RESULTS_TIMEOUT = 120
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


def _generated_dir(execution_id: str) -> Path:
    base = Path(settings.artifacts_dir)
    if not base.is_absolute():
        base = Path.cwd() / base
    generated_dir = base / execution_id / "archivos_generados"
    generated_dir.mkdir(parents=True, exist_ok=True)
    return generated_dir


def _save_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def resolve_testcase_file(testcase_ref: str) -> Path:
    """Resuelve un TestCase del body a su fichero real (normalmente en examples/).

    Los TestCases NO se re-renderizan: se suben tal cual desde `examples/` para no
    corromper el entrecomillado ni la indentación (ELCM es muy sensible a la
    sintaxis). Fail-fast si el fichero no existe.
    """
    candidate = Path(testcase_ref)
    if candidate.exists() and candidate.is_file():
        return candidate.resolve()

    resolved = _resolve_examples_path(testcase_ref)
    if resolved and Path(resolved).is_file():
        return Path(resolved).resolve()

    raise FileNotFoundError(f"TestCase file not found: {testcase_ref}")


def extract_testcase_name(testcase_path: str) -> str:
    """Devuelve el `Name:` interno de un TestCase V2 (ELCM los registra por Name).

    El descriptor debe referenciar cada TestCase por su `Name:` interno, no por el
    nombre de fichero: ELCM registra los TestCases V2 por ese campo. Fail-fast si
    el fichero no es un TestCase válido o no declara `Name`.
    """
    path = Path(testcase_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("Name"):
        raise ValueError(f"TestCase '{path.name}' has no 'Name' field")
    return str(data["Name"])


def _find_task_config(node: Any, task_name: str) -> dict | None:
    """Busca recursivamente el `Config` del primer task `task_name` en la estructura.

    Recorre dicts y listas (p. ej. `Sequence` y los `Children` de Flow.Parallel/
    Flow.Sequence) hasta dar con `{"Task": task_name, "Config": {...}}`.
    """
    if isinstance(node, dict):
        if node.get("Task") == task_name and isinstance(node.get("Config"), dict):
            return node["Config"]
        for value in node.values():
            found = _find_task_config(value, task_name)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_task_config(item, task_name)
            if found is not None:
                return found
    return None


def extract_capture_metrics(testcase_files: list[str]) -> tuple[str, list[str]] | None:
    """Lee measurement + métricas del TestCase de captura del experimento.

    Identifica la captura entre `testcase_files` (rutas ya resueltas a `examples/`)
    por convención: el nombre de fichero contiene `_capture` y el TestCase incluye un
    `Run.PrometheusToInflux`. Devuelve `(measurement, [métricas de nombre simple])`;
    las queries con agregación (p. ej. `sum(rate(...))`) se descartan porque su `Field`
    saneado no sirve para un panel Grafana. `None` si no hay captura válida.
    """
    for testcase_file in testcase_files:
        path = Path(testcase_file)
        if "_capture" not in path.name.lower():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            logger.debug("Skipping unparseable capture candidate %s: %s", path.name, exc)
            continue

        config = _find_task_config(data, "Run.PrometheusToInflux")
        if not isinstance(config, dict):
            continue

        measurement = config.get("Measurement")
        queries = config.get("QueriesRange") or []
        metrics = [q for q in queries if isinstance(q, str) and re.fullmatch(r"[A-Za-z0-9_]+", q)]
        if measurement and metrics:
            return str(measurement), metrics
    return None


async def generate_experiment_descriptor(
    experiment: ExperimentConfig,
    testcase_paths: list[str],
    execution_id: str,
) -> str:
    """Genera el Experiment Descriptor de ELCM para un experimento concreto.

    Como el TN Descriptor, se genera por ejecución a partir de la plantilla base
    (`templates/ELCM/template_experiment_descriptor.json`) rellenando la lista de
    TestCases y UEs propia de este experimento (no se usa `ytt`, es JSON puro).
    Se guarda junto al TN Descriptor, en artifacts/<id>/archivos_generados/, con
    un nombre por experimento (`experiment_descriptor_<experimento>.json`) para
    que varios experimentos sobre la misma TN no se pisen.
    """
    template_path = resolve_template_path(
        "ELCM/template_experiment_descriptor.json", category="ELCM"
    )
    if template_path is None:
        raise FileNotFoundError(
            "Experiment descriptor template not found: ELCM/template_experiment_descriptor.json"
        )

    payload = json.loads(template_path.read_text(encoding="utf-8"))
    payload["Application"] = experiment.name
    # Referenciar cada TestCase por su Name interno (ELCM los registra por Name).
    payload["TestCases"] = [extract_testcase_name(path) for path in testcase_paths if path]
    payload["UEs"] = list(experiment.ues_paths)

    filename = f"experiment_descriptor_{artifacts._sanitize_path_component(experiment.name)}.json"
    output_path = _generated_dir(execution_id) / filename
    _save_text(output_path, json.dumps(payload, indent=4, ensure_ascii=False))
    logger.info("[%s] Experiment descriptor generated: %s", execution_id, output_path)
    return str(output_path)


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

            resolved_path = (
                testcase_path
                if Path(testcase_path).exists()
                else _resolve_examples_path(testcase_path)
            )
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

            telemetry.increment_counter(
                "requests_total", labels={"service": "elcm", "operation": "upload"}
            )
            upload_timer = telemetry.start_timer(
                "elcm", "upload", telemetry.ensure_execution_id(execution_id)
            )
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
                telemetry.increment_counter(
                    "errors_total",
                    labels={
                        "service": "elcm",
                        "operation": "upload",
                        "error_type": str(exc.response.status_code),
                    },
                )
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
    *,
    exp_descriptor_path: str,
) -> str:
    """Launch the experiment descriptor in ELCM and return execution_id.

    El descriptor se genera por experimento (`generate_experiment_descriptor`) y
    se pasa siempre de forma explícita: ya no hay fallback a `examples/`.
    """
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
            telemetry.increment_counter(
                "errors_total",
                labels={"service": "elcm", "operation": "run", "error_type": str(status_code)},
            )
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
    status_timer = telemetry.start_timer(
        "elcm", "status", execution_id=telemetry.ensure_execution_id(execution_id or experiment_id)
    )
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
            telemetry.increment_counter(
                "errors_total",
                labels={
                    "service": "elcm",
                    "operation": "status",
                    "error_type": str(exc.response.status_code),
                },
            )
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
    telemetry.increment_counter(
        "requests_total", labels={"service": "elcm", "operation": "collect"}
    )
    collect_timer = telemetry.start_timer(
        "elcm", "collect", execution_id=telemetry.ensure_execution_id(execution_id or experiment_id)
    )
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
                logger.info(
                    f"Logs not ready yet for experiment {experiment_id}, returning empty logs"
                )
                return {
                    "output": "logs",
                    "experiment_id": experiment_id,
                    "logs": {"message": "Logs not available yet"},
                    "status": "logs_pending",
                }
            _log_http_response("ELCM", exc.response)
            detail = _response_error_detail(exc.response)
            telemetry.increment_counter(
                "errors_total",
                labels={
                    "service": "elcm",
                    "operation": "collect",
                    "error_type": str(exc.response.status_code),
                },
            )
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


class ElcmResultsNotFoundError(RuntimeError):
    """Raised when ELCM has no results ZIP for the execution (HTTP 404)."""


async def download_execution_results(
    experiment_id: str,
    dest_path: str,
    elcm_base_url: str | None = None,
    execution_id: str | None = None,
) -> str:
    """Descargar el ZIP de resultados de ELCM a `dest_path`.

    GET {base}/elcm/api/v1/execution/{experiment_id}/results

    El endpoint devuelve `{id}.zip` como adjunto (logs planos + ZIP interno con
    el CSV, ver Compress.Zip flat=True en el backend). La red va con httpx async
    y la escritura a disco se saca del event loop (§8.1).

    Raises:
        ElcmResultsNotFoundError: si ELCM responde 404 (sin resultados).
        RuntimeError: para otros errores HTTP.
    """
    base_url = _resolve_elcm_url(elcm_base_url)
    telemetry.increment_counter(
        "requests_total", labels={"service": "elcm", "operation": "results"}
    )
    results_timer = telemetry.start_timer(
        "elcm", "results", execution_id=telemetry.ensure_execution_id(execution_id or experiment_id)
    )
    results_timer.start()
    results_status = "success"

    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=ELCM_RESULTS_TIMEOUT) as client:
        try:
            response = await client.get(
                f"{base_url}/elcm/api/v1/execution/{experiment_id}/results",
                headers={"Accept": "*/*"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            telemetry.increment_counter(
                "errors_total",
                labels={"service": "elcm", "operation": "results", "error_type": str(status_code)},
            )
            results_status = "error"
            if status_code == 404:
                raise ElcmResultsNotFoundError(
                    f"ELCM has no results for execution {experiment_id} (HTTP 404)."
                ) from exc
            detail = _response_error_detail(exc.response)
            raise RuntimeError(
                f"ELCM results request failed for execution {experiment_id} "
                f"(HTTP {status_code}). Backend error: {detail or 'unknown'}"
            ) from exc
        finally:
            try:
                results_timer.stop(status=results_status)
            except Exception:
                pass

        content = response.content
        await asyncio.to_thread(dest.write_bytes, content)
        logger.info(
            "ELCM results downloaded for execution %s: %s (%d bytes)",
            experiment_id,
            dest,
            len(content),
        )
        return str(dest)
