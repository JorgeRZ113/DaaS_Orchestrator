import asyncio
import json
import re
from pathlib import Path

from app import artifacts
from app.config import settings
from app.utils.telemetry import telemetry

# Pattern for HH:MM:SS-DD/MM/AAAA format
TIMESTAMP_PATTERN = re.compile(r"^\d{2}:\d{2}:\d{2}-\d{2}/\d{2}/\d{4}$")


def test_build_artifacts_generates_logs_and_metadata(tmp_path) -> None:
    previous_dir = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)

    try:
        output_paths = asyncio.run(
            artifacts.build_artifacts(
                execution_id="exec-1",
                tn_id="tn-demo",
                experiment_id="exp-1",
                results={
                    "testcases": ["TestCase_ping.yml", "TestCase_iperf.yml"],
                    "logs": [{"line": "ok"}],
                },
            )
        )
    finally:
        settings.artifacts_dir = previous_dir

    assert len(output_paths) == 2

    metadata = json.loads(Path(output_paths[0]).read_text(encoding="utf-8"))
    logs = json.loads(Path(output_paths[1]).read_text(encoding="utf-8"))

    assert metadata["tn_id"] == "tn-demo"
    assert metadata["output"] == "logs"
    assert metadata["testcases_count"] == 2
    assert TIMESTAMP_PATTERN.match(
        metadata["generated_at"]
    ), f"Invalid timestamp format: {metadata['generated_at']}"
    assert logs == [{"line": "ok"}]


def test_build_tnlcm_report_files_are_created(tmp_path) -> None:
    previous_dir = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)

    try:
        raw_path = asyncio.run(
            artifacts.build_tnlcm_raw_report_artifact(
                execution_id="exec-2",
                report_markdown="# Report\nstatus: ok",
            )
        )
        summary_path = asyncio.run(
            artifacts.build_tnlcm_summary_artifact(
                execution_id="exec-2",
                tn_id="tn-demo",
                report_summary={"components": {"elcm": {"ip": "10.0.0.1"}}},
            )
        )
    finally:
        settings.artifacts_dir = previous_dir

    assert Path(raw_path).exists()
    assert Path(summary_path).exists()

    summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    assert summary["tn_id"] == "tn-demo"
    assert "summary" in summary


def test_build_telemetry_report_artifact_is_created(tmp_path) -> None:
    previous_dir = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)
    telemetry.reset()

    try:
        telemetry.increment_counter(
            "requests_total", labels={"service": "tests", "operation": "artifact"}
        )
        telemetry.observe_duration(
            service="tnlcm",
            operation="activate",
            execution_id="exec-3",
            duration_seconds=0.5,
        )
        telemetry.observe_duration(
            service="tnlcm",
            operation="destroy",
            execution_id="exec-3",
            duration_seconds=65.034,
        )
        telemetry_path = asyncio.run(
            artifacts.build_telemetry_report_artifact(
                execution_id="exec-3",
                stage="tnlcm_completed",
            )
        )
    finally:
        telemetry.reset()
        settings.artifacts_dir = previous_dir

    snapshot_file = Path(telemetry_path)
    assert snapshot_file.exists()
    payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert payload["metadata"]["execution_id"] == "exec-3"
    assert payload["metadata"]["stage"] == "tnlcm_completed"
    assert payload["metadata"]["snapshot_type"] == "telemetry_report"
    assert TIMESTAMP_PATTERN.match(
        payload["metadata"]["generated_at"]
    ), f"Invalid timestamp format: {payload['metadata']['generated_at']}"
    assert any(metric["name"] == "requests_total" for metric in payload["counters"])
    assert any(timing["operation"] == "destroy" for timing in payload["timings"])
    assert all(timing["duration_seconds"] >= 1.0 for timing in payload["timings"])
    assert payload["timings"][0]["duration_display"] == "01:05:034"
    assert payload["totals"]["tnlcm"]["destruccion"]["count"] == 1
    assert payload["totals"]["tnlcm"]["destruccion"]["duration_display"] == "01:05:034"
    assert payload["totals"]["tnlcm"]["activacion"]["count"] == 1
    assert payload["totals"]["tnlcm"]["activacion"]["duration_display"] == "00:00:500"
