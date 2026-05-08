import pytest

from app import config


def test_load_settings_telemetry_report_artifacts_default_true() -> None:
    loaded = config._load_settings({})
    assert loaded.telemetry_report_artifacts is True


def test_load_settings_telemetry_report_artifacts_can_be_false() -> None:
    loaded = config._load_settings({"TELEMETRY_REPORT_ARTIFACTS": "false"})
    assert loaded.telemetry_report_artifacts is False


def test_load_settings_telemetry_report_artifacts_invalid_value_raises() -> None:
    with pytest.raises(ValueError, match="TELEMETRY_REPORT_ARTIFACTS"):
        config._load_settings({"TELEMETRY_REPORT_ARTIFACTS": "maybe"})


def test_reload_mutable_settings_updates_telemetry_report_flag(monkeypatch) -> None:
    previous = config.settings.telemetry_report_artifacts

    try:
        # Force current runtime value and simulate .env reloading to opposite value.
        config.settings.telemetry_report_artifacts = True
        monkeypatch.setattr(config, "dotenv_values", lambda _path: {"TELEMETRY_REPORT_ARTIFACTS": "false"})

        result = config.reload_mutable_settings()

        assert "telemetry_report_artifacts" in result["updated_fields"]
        assert config.settings.telemetry_report_artifacts is False
    finally:
        config.settings.telemetry_report_artifacts = previous

