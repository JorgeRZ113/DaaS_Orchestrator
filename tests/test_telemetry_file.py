import json
from pathlib import Path

from app.utils.telemetry import telemetry
from app.config import settings


def test_telemetry_appends_to_single_file(tmp_path) -> None:
    prev = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)
    try:
        execution_id = "exec-telemetry-file"
        path = Path(settings.artifacts_dir) / "tests" / execution_id / "telemetry.log"
        if path.exists():
            path.unlink()

        telemetry.log_event(
            "info",
            "unit.test.event",
            service="test",
            operation="write",
            execution_id=execution_id,
        )

        assert path.exists()
        content = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(content) >= 1
        last = json.loads(content[-1])
        assert last.get("message") == "unit.test.event"
        assert last.get("service") == "test"
        # Marca ISO ademas de la humana: es la que permite ordenar los eventos.
        assert last.get("ts", "").startswith("20")
    finally:
        settings.artifacts_dir = prev


def test_timer_stop_with_error_status_emits_a_failed_message(tmp_path) -> None:
    """Un paso fallido no puede anunciarse como `.completed`."""
    prev = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)
    try:
        execution_id = "exec-timer-failed"
        timer = telemetry.start_timer("orchestrator", "tnlcm_phase", execution_id)
        timer.start()
        timer.stop(status="error")

        path = Path(settings.artifacts_dir) / "tests" / execution_id / "telemetry.log"
        last = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert last["message"] == "orchestrator.tnlcm_phase.failed"
        assert last["phase"] == "error"
        assert last["status"] == "error"
    finally:
        settings.artifacts_dir = prev


def test_start_timer_without_execution_id_creates_no_artifact_directory(tmp_path) -> None:
    """Regresion: acunar un UUID por medida dejaba cientos de carpetas huerfanas."""
    prev = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)
    try:
        timer = telemetry.start_timer("tnlcm", "destroy")
        timer.start()
        timer.stop(status="success")

        root = Path(settings.artifacts_dir) / "tests"
        assert [child.name for child in root.iterdir() if child.is_dir()] == []
        assert (root / "telemetry.log").exists()
    finally:
        settings.artifacts_dir = prev
