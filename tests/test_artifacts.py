import asyncio
import json
from pathlib import Path

from app import artifacts
from app.config import settings


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
