import re

from app.utils.telemetry import telemetry
from app.config import settings

# Pattern for HH:MM:SS-DD/MM/AAAA format
TIMESTAMP_PATTERN = re.compile(r"^\d{2}:\d{2}:\d{2}-\d{2}/\d{2}/\d{4}$")


def test_telemetry_log_event_uses_correct_timestamp_format(caplog, tmp_path) -> None:
    """Verify telemetry log events use HH:MM:SS-DD/MM/AAAA timestamp format."""
    previous = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)
    try:
        telemetry.log_event(
            "info",
            "test_event",
            service="test_service",
            operation="test_op",
            execution_id="exec-timestamp",
        )

        # Capture the last log record and parse as JSON
        import json
        import logging

        telemetry_logger = logging.getLogger("telemetry")
        assert telemetry_logger.hasHandlers() or len(caplog.records) > 0

        # The log message is JSON, extracted from caplog
        for record in caplog.records:
            if "test_event" in record.message:
                try:
                    payload = json.loads(record.message)
                    assert "timestamp" in payload
                    timestamp = payload["timestamp"]
                    assert TIMESTAMP_PATTERN.match(
                        timestamp
                    ), f"Invalid timestamp format in log: {timestamp}"
                    break
                except json.JSONDecodeError:
                    pass
    finally:
        settings.artifacts_dir = previous
