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
    finally:
        settings.artifacts_dir = prev

