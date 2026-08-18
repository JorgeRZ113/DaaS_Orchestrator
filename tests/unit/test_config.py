import pytest

from app.core import config


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
    # Snapshot completo de los campos recargables: reload_mutable_settings muta el
    # `settings` global y, con un .env simulado parcial, resetearía el resto de
    # campos (api_key, tnlcm_url, ...) a sus defaults, contaminando otros tests.
    snapshot = {
        field: getattr(config.settings, field) for field in config.RELOADABLE_SETTING_FIELDS
    }

    try:
        # Simula recargar el .env manteniendo los valores actuales y solo
        # alternando TELEMETRY_REPORT_ARTIFACTS, para no tocar api_key ni URLs.
        config.settings.telemetry_report_artifacts = True
        fake_env = {
            "APP_ENV": config.settings.app_env,
            "API_KEY": config.settings.api_key,
            "TNLCM_URL": config.settings.tnlcm_url,
            "TNLCM_USER": config.settings.tnlcm_user,
            "TNLCM_PASSWORD": config.settings.tnlcm_password,
            "LOG_LEVEL": config.settings.log_level,
            "TELEMETRY_REPORT_ARTIFACTS": "false",
        }
        monkeypatch.setattr(config, "dotenv_values", lambda _path: fake_env)

        result = config.reload_mutable_settings()

        assert result["updated_fields"] == ["telemetry_report_artifacts"]
        assert config.settings.telemetry_report_artifacts is False
    finally:
        for field, value in snapshot.items():
            setattr(config.settings, field, value)
