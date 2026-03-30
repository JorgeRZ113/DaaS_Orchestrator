import json
import logging
import os
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
    """
    base_dir = os.path.join(settings.artifacts_dir, execution_id)
    _ensure_dir(base_dir)

    # Signature compatibility: experiment_id is kept although metadata is now minimal.
    _ = experiment_id

    logs_payload = results.get("logs") if isinstance(results, dict) else results
    if logs_payload is None:
        logs_payload = results

    testcases_count = 0
    if isinstance(results, dict) and isinstance(results.get("testcases"), list):
        testcases_count = len([tc for tc in results.get("testcases", []) if isinstance(tc, str) and tc])
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
        "generated_at": datetime.now(timezone.utc).isoformat(),
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
    base_dir = os.path.join(settings.artifacts_dir, execution_id)
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
    """Persist TNLCM parsed summary in template_tnlcm_report_summary.json."""
    base_dir = os.path.join(settings.artifacts_dir, execution_id)
    _ensure_dir(base_dir)

    summary_data = {
        "tn_id": tn_id,
        "summary": _json_safe(report_summary),
    }
    summary_path = os.path.join(base_dir, "template_tnlcm_report_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2)

    logger.info(f"[{execution_id}] TNLCM summary report generated")
    return summary_path

