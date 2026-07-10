import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.models import DatasetDescriptor
from app.utils.telemetry import telemetry

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
) -> list[str]:
    """
    Build artifacts for the current dataset mode (logs only):
    - metadata.json
    - logs.json
    """
    base_dir = _artifact_base_dir(execution_id)
    _ensure_dir(base_dir)

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
        "output": "logs",
        "generated_at": _format_timestamp_human(),
        "testcases_count": testcases_count,
    }
    metadata_path = os.path.join(base_dir, "metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    logger.info(f"[{execution_id}] metadata.json generated")

    logs_path = os.path.join(base_dir, "logs.json")
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


def persist_dataset_descriptor(execution_id: str, descriptor: DatasetDescriptor) -> str:
    """
    Persists the DatasetDescriptor to a JSON file in the artifact directory.

    Excludes temporary fields like descriptor_path (only needed for template resolution during generation).
    """
    base_dir = _artifact_base_dir(execution_id)
    _ensure_dir(base_dir)
    descriptor_path = os.path.join(base_dir, "dataset_descriptor.json")

    # Exclude descriptor_path: it's only used for template resolution during generation,
    # not needed in persisted state (descriptor is already generated/available)
    descriptor_dict = descriptor.model_dump(
        exclude={"infrastructure": {"descriptor_path"}},
        exclude_none=False
    )

    with open(descriptor_path, "w", encoding="utf-8") as f:
        json.dump(descriptor_dict, f, indent=2)

    logger.info(f"[{execution_id}] dataset_descriptor.json saved")
    return descriptor_path


def load_dataset_descriptor(execution_id: str) -> DatasetDescriptor:
    """Loads a previously persisted DatasetDescriptor from the artifact directory."""
    descriptor_path = os.path.join(_artifact_base_dir(execution_id), "dataset_descriptor.json")
    if not os.path.exists(descriptor_path):
        raise FileNotFoundError(f"Descriptor not found for execution {execution_id}")
    with open(descriptor_path, "r", encoding="utf-8") as f:
        return DatasetDescriptor.model_validate_json(f.read())
