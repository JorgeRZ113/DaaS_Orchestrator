import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.core.config import settings
from app.domain.descriptor import (
    DatasetDescriptor,
    DatasetRequest,
    DescriptorSource,
    ExperimentConfig,
)
from app.domain.execution import ExecutionRecord
from app.observability.execution_summary import build_execution_summary, render_summary_markdown
from app.observability.telemetry import telemetry

logger = logging.getLogger(__name__)


def _artifact_root_dir() -> str:
    base = settings.artifacts_dir or "./artifacts"
    if os.getenv("PYTEST_CURRENT_TEST"):
        base = os.path.join(base, "tests")
    return base


def _artifact_base_dir(execution_id: str) -> str:
    return os.path.join(_artifact_root_dir(), execution_id)


def _artifact_generated_dir(execution_id: str) -> str:
    return os.path.join(_artifact_base_dir(execution_id), "archivos_generados")


def _sanitize_path_component(name: str) -> str:
    """Sanea un nombre para usarlo como componente de ruta (evita path traversal).

    Sustituye cualquier carácter fuera de `[A-Za-z0-9_.-]` por `_` y recorta los
    `_` sobrantes. Devuelve `experiment` si el resultado queda vacío.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
    return safe or "experiment"


def _artifact_result_dir(execution_id: str, experiment_name: str | None = None) -> str:
    """Directorio de respuestas del dataset: artifacts/<id>/result/[<experimento>/].

    Todas las respuestas obtenidas de la parte `dataset` (logs, csv, dashboard,
    raw) se guardan aquí, dentro de la carpeta de la ejecución de la TN. Como una
    misma TN puede ejecutar varios experimentos (cada uno con su propia salida de
    datos), las respuestas se separan por el nombre del experimento cuando se
    indica: `result/<experimento>/`.
    """
    result_dir = os.path.join(_artifact_base_dir(execution_id), "result")
    if experiment_name:
        return os.path.join(result_dir, _sanitize_path_component(experiment_name))
    return result_dir


def persist_generated_artifacts(
    execution_id: str,
    tnlcm_descriptor_path: str | None = None,
    experiment_descriptor_path: str | None = None,
    testcase_paths: list[str] | None = None,
) -> list[str]:
    """Register generated artifact paths for later traceability checks."""
    _ensure_dir(_artifact_generated_dir(execution_id))

    paths: list[str] = []
    for candidate in [tnlcm_descriptor_path, experiment_descriptor_path]:
        if candidate and Path(candidate).exists():
            paths.append(candidate)

    if testcase_paths:
        for testcase_path in testcase_paths:
            if testcase_path and Path(testcase_path).exists():
                paths.append(testcase_path)

    return list(dict.fromkeys(paths))


def _format_timestamp_human() -> str:
    """Generar timestamp en formato HH:MM:SS-DD/MM/AAAA (hora local o UTC según necesidad)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%H:%M:%S-%d/%m/%Y")


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


async def build_artifacts(
    execution_id: str,
    tn_id: str,
    experiment_id: str,
    results: dict[str, Any],
    experiment_name: str | None = None,
) -> list[str]:
    """
    Build artifacts for the logs dataset mode, en artifacts/<id>/result/[<experimento>/]:
    - metadata.json
    - logs.json
    """
    result_dir = _artifact_result_dir(execution_id, experiment_name)
    _ensure_dir(result_dir)

    # Signature compatibility: experiment_id is kept although metadata is now minimal.
    _ = experiment_id

    logs_payload = results.get("logs") if isinstance(results, dict) else results
    if logs_payload is None:
        logs_payload = results

    testcases_count = 0
    if isinstance(results, dict) and isinstance(results.get("testcases"), list):
        testcases_count = len(
            [tc for tc in results.get("testcases", []) if isinstance(tc, str) and tc]
        )
    elif isinstance(logs_payload, list):
        seen_testcases: set[str] = set()
        for entry in logs_payload:
            if not isinstance(entry, dict):
                continue

            testcase = entry.get("testcase")
            if isinstance(testcase, str) and testcase:
                seen_testcases.add(testcase)

        testcases_count = len(seen_testcases)

    metadata = {
        "tn_id": tn_id,
        "experiment": experiment_name,
        "output": "logs",
        "generated_at": _format_timestamp_human(),
        "testcases_count": testcases_count,
    }
    metadata_path = os.path.join(result_dir, "metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    logger.info(f"[{execution_id}] metadata.json generated")

    logs_path = os.path.join(result_dir, "logs.json")
    with open(logs_path, "w", encoding="utf-8") as f:
        json.dump(logs_payload, f, indent=2)
    logger.info(f"[{execution_id}] logs.json generated")

    return [metadata_path, logs_path]


async def build_tnlcm_raw_report_artifact(
    execution_id: str,
    report_markdown: str,
) -> str:
    """Persist TNLCM raw report as markdown (.md)."""
    base_dir = _artifact_base_dir(execution_id)
    _ensure_dir(base_dir)

    report_path = os.path.join(base_dir, "tnlcm_report_raw.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_markdown or "")

    logger.info(f"[{execution_id}] TNLCM raw markdown report generated")
    return report_path


async def build_tnlcm_summary_artifact(
    execution_id: str,
    tn_id: str,
    report_summary: dict[str, Any],
) -> str:
    """Persist TNLCM parsed summary in tnlcm_report_summary.json."""
    base_dir = _artifact_base_dir(execution_id)
    _ensure_dir(base_dir)

    summary_data = {
        "tn_id": tn_id,
        "summary": _json_safe(report_summary),
    }
    summary_path = os.path.join(base_dir, "tnlcm_report_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2)

    logger.info(f"[{execution_id}] TNLCM summary report generated")
    return summary_path


def _stage_status_from_name(stage: str) -> str:
    if stage.endswith("_completed"):
        return "success"
    if stage.endswith("_failed"):
        return "error"
    if stage.endswith("_finalized"):
        return "finalized"
    return "unknown"


async def build_telemetry_report_artifact(
    execution_id: str,
    stage: str,
) -> str:
    """Persist an in-memory telemetry report next to execution artifacts."""
    base_dir = _artifact_base_dir(execution_id)
    _ensure_dir(base_dir)

    safe_stage = (stage or "unknown").strip().lower().replace(" ", "_")
    telemetry_path = os.path.join(base_dir, f"telemetry_report_{safe_stage}.json")
    payload = telemetry.telemetry_report(
        execution_id=execution_id,
        stage=safe_stage,
        status=_stage_status_from_name(safe_stage),
    )
    with open(telemetry_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    logger.info(f"[{execution_id}] telemetry report generated: {telemetry_path}")
    return telemetry_path


async def build_execution_summary_artifacts(
    execution_id: str,
    record: ExecutionRecord,
) -> list[str]:
    """Persist the experimenter-facing summary as summary.json + summary.md.

    Es la contraparte legible de `build_telemetry_report_artifact`: mismo
    directorio, pero con el vocabulario y las duraciones que entiende quien
    lanza el experimento.
    """
    base_dir = _artifact_base_dir(execution_id)
    _ensure_dir(base_dir)

    summary = build_execution_summary(execution_id, record)

    summary_json_path = os.path.join(base_dir, "summary.json")
    with open(summary_json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    summary_md_path = os.path.join(base_dir, "summary.md")
    with open(summary_md_path, "w", encoding="utf-8") as f:
        f.write(render_summary_markdown(summary))

    logger.info(f"[{execution_id}] execution summary generated: {summary_md_path}")
    return [summary_json_path, summary_md_path]


def _dump_yaml(data: Any) -> str:
    """Serializa a YAML legible, como lo escribiria una persona.

    Deliberadamente NO usa `rendering.yaml_style`: aquel entrecomilla todos los
    valores y convierte los nulos en cadenas vacias porque lo exige el parser de
    ELCM, y aplicarlo aqui produciria un descriptor incomodo de leer y de editar.
    """
    return yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        indent=2,
    )


def persist_dataset_descriptor(
    execution_id: str,
    descriptor: DatasetDescriptor,
    source: DescriptorSource | None = None,
) -> list[str]:
    """Deja constancia del DatasetDescriptor que origino la ejecucion.

    El YAML se escribe siempre, porque es el formato que el anteproyecto define
    para el descriptor. El JSON solo se anade cuando el JSON fue lo que se envio:
    asi queda la peticion tal cual llego sin ensuciar el directorio con una
    traduccion que nadie pidio.

    Cuando el origen es YAML se guarda el texto **verbatim**. Reserializar desde
    el modelo perderia los comentarios, que son lo que aporta YAML frente a JSON
    y lo que convierte este fichero en documentacion del experimento.

    Devuelve las rutas escritas, en orden de relevancia (el YAML primero).
    """
    base_dir = _artifact_base_dir(execution_id)
    _ensure_dir(base_dir)

    # Exclude descriptor_path: it's only used for template resolution during
    # generation, not needed in persisted state.
    descriptor_dict = descriptor.model_dump(
        exclude={"infrastructure": {"descriptor_path"}}, exclude_none=False
    )

    written: list[str] = []

    yaml_path = os.path.join(base_dir, "dataset_descriptor.yaml")
    if source is not None and source.is_yaml and source.raw is not None:
        yaml_content = source.raw
    else:
        yaml_content = _dump_yaml(descriptor_dict)
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(yaml_content)
    written.append(yaml_path)
    logger.info(f"[{execution_id}] dataset_descriptor.yaml saved")

    if source is None or not source.is_yaml:
        json_path = os.path.join(base_dir, "dataset_descriptor.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(descriptor_dict, f, indent=2)
        written.append(json_path)
        logger.info(f"[{execution_id}] dataset_descriptor.json saved")

    return written


def persist_experiment_request(
    execution_id: str,
    experiment: ExperimentConfig,
    dataset: DatasetRequest,
    source: DescriptorSource | None = None,
) -> str:
    """Deja constancia de con que se lanzo cada experimento ELCM.

    Una misma TN admite varios experimentos, cada uno con su propia salida de
    datos, asi que el fichero vive junto a los resultados de ese experimento. Sin
    esto la traza queda incompleta: se sabe que se obtuvo, pero no que se pidio.
    """
    result_dir = _artifact_result_dir(execution_id, experiment.name)
    _ensure_dir(result_dir)
    request_path = os.path.join(result_dir, "experiment_request.yaml")

    if source is not None and source.is_yaml and source.raw is not None:
        content = source.raw
    else:
        content = _dump_yaml(
            {
                "experiment": experiment.model_dump(),
                "dataset": dataset.model_dump(exclude_none=True),
            }
        )

    with open(request_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info(f"[{execution_id}] experiment_request.yaml saved for '{experiment.name}'")
    return request_path


def load_dataset_descriptor(execution_id: str) -> DatasetDescriptor:
    """Loads a previously persisted DatasetDescriptor from the artifact directory.

    Prioriza el YAML, que es el formato actual, y cae al JSON para poder leer
    ejecuciones anteriores al cambio.
    """
    base_dir = _artifact_base_dir(execution_id)

    yaml_path = os.path.join(base_dir, "dataset_descriptor.yaml")
    if os.path.exists(yaml_path):
        with open(yaml_path, "r", encoding="utf-8") as f:
            return DatasetDescriptor.model_validate(yaml.safe_load(f.read()))

    json_path = os.path.join(base_dir, "dataset_descriptor.json")
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            return DatasetDescriptor.model_validate_json(f.read())

    raise FileNotFoundError(f"Descriptor not found for execution {execution_id}")


def load_tnlcm_report_summary(execution_id: str) -> dict[str, Any]:
    """Carga el `summary` del report TNLCM (artifacts/<id>/tnlcm_report_summary.json)."""
    summary_path = os.path.join(_artifact_base_dir(execution_id), "tnlcm_report_summary.json")
    if not os.path.exists(summary_path):
        raise FileNotFoundError(f"TNLCM report summary not found for execution {execution_id}")
    with open(summary_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get("summary"), dict):
        return data["summary"]
    return {}


def load_monitoring_info(execution_id: str) -> dict[str, Any]:
    """Extrae el bloque `monitoring` del report TNLCM persistido.

    Devuelve ip/ports y credenciales (token/organization/bucket) tal como los
    reporta TNLCM. IMPORTANTE: el `token` es secreto; usarlo solo en memoria y
    NO re-persistirlo en executions.json ni en artifacts (§8.7).
    """
    summary = load_tnlcm_report_summary(execution_id)
    monitoring = summary.get("monitoring") if isinstance(summary, dict) else None
    return monitoring if isinstance(monitoring, dict) else {}
