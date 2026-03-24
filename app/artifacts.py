import json
import logging
import os
import zipfile
from datetime import datetime, timezone
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


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
    - dataset-logs-<execution_id>-<timestamp>.zip
    """
    base_dir = os.path.join(settings.artifacts_dir, execution_id)
    _ensure_dir(base_dir)

    metadata = {
        "execution_id": execution_id,
        "tn_id": tn_id,
        "experiment_id": experiment_id,
        "output": "logs",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata_path = os.path.join(base_dir, "metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    logger.info(f"[{execution_id}] metadata.json generated")

    logs_payload = results.get("logs") if isinstance(results, dict) else results
    if logs_payload is None:
        logs_payload = results

    logs_path = os.path.join(base_dir, "logs.json")
    with open(logs_path, "w", encoding="utf-8") as f:
        json.dump(logs_payload, f, indent=2)
    logger.info(f"[{execution_id}] logs.json generated")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    zip_name = f"dataset-logs-{execution_id}-{timestamp}.zip"
    zip_path = os.path.join(base_dir, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(metadata_path, "metadata.json")
        zf.write(logs_path, "logs.json")
    logger.info(f"[{execution_id}] {zip_name} generated")

    return [metadata_path, logs_path, zip_path]


async def build_tnlcm_report_artifacts(
    execution_id: str,
    tn_id: str,
    report_payload: dict[str, Any],
    report_summary: dict[str, Any],
) -> list[str]:
    """Persist TNLCM report files after activate phase."""
    base_dir = os.path.join(settings.artifacts_dir, execution_id)
    _ensure_dir(base_dir)

    report_path = os.path.join(base_dir, "tnlcm_report_raw.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(_json_safe(report_payload), f, indent=2)

    summary_data = {
        "tn_id": tn_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": _json_safe(report_summary),
    }
    summary_path = os.path.join(base_dir, "tnlcm_report_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2)

    logger.info(f"[{execution_id}] TNLCM report artifacts generated")
    return [report_path, summary_path]

