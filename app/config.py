import os
from threading import Lock
from typing import Any, Mapping

from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel

load_dotenv()


ALLOWED_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

RELOADABLE_SETTING_FIELDS = frozenset(
    {
        "app_env",
        "api_key",
        "tnlcm_url",
        "tnlcm_user",
        "tnlcm_password",
        "tnlcm_token",
        "elcm_url",
        "request_timeout",
        "tnlcm_activate_timeout",
        "tnlcm_activate_retry_delay",
        "tnlcm_activate_redeploy_max_attempts",
        "tnlcm_redeploy_delay",
        "tnlcm_recovery_destroy_delay",
        "tnlcm_report_timeout",
        "poll_interval",
        "log_level",
    }
)

NON_RELOADABLE_SETTING_FIELDS = frozenset(
    {
        "app_host",
        "app_port",
        "executions_file",
        "artifacts_dir",
        "examples_dir",
    }
)


class Settings(BaseModel):
    app_env: str
    app_host: str
    app_port: int

    api_key: str

    tnlcm_url: str
    tnlcm_user: str
    tnlcm_password: str
    tnlcm_token: str

    elcm_url: str

    request_timeout: int
    tnlcm_activate_timeout: int
    tnlcm_activate_retry_delay: int
    tnlcm_activate_redeploy_max_attempts: int
    tnlcm_redeploy_delay: int
    tnlcm_recovery_destroy_delay: int
    tnlcm_report_timeout: int
    poll_interval: int

    executions_file: str
    artifacts_dir: str
    examples_dir: str
    log_level: str


def _read_int(values: Mapping[str, Any] | None, name: str, default: int) -> int:
    raw = _read_str(values, name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {name}: {raw}") from exc


def _read_str(values: Mapping[str, Any] | None, name: str, default: str) -> str:
    if values is None:
        raw = os.getenv(name, default)
    else:
        raw = values.get(name, default)
        if raw is None:
            raw = default
    return str(raw).strip()


def _read_url(values: Mapping[str, Any] | None, name: str, default: str) -> str:
    # Normaliza para evitar dobles barras al concatenar rutas (/api/...)
    return _read_str(values, name, default).rstrip("/")


def _load_settings(values: Mapping[str, Any] | None = None) -> Settings:
    return Settings(
        app_env=_read_str(values, "APP_ENV", "dev"),
        app_host=_read_str(values, "APP_HOST", "0.0.0.0"),
        app_port=_read_int(values, "APP_PORT", 8000),
        api_key=_read_str(values, "API_KEY", "changeme"),
        tnlcm_url=_read_url(values, "TNLCM_URL", "http://localhost:5000"),
        tnlcm_user=_read_str(values, "TNLCM_USER", ""),
        tnlcm_password=_read_str(values, "TNLCM_PASSWORD", ""),
        tnlcm_token=_read_str(values, "TNLCM_TOKEN", "changeme"),
        elcm_url=_read_url(values, "ELCM_URL", "http://localhost:8080"),
        request_timeout=_read_int(values, "REQUEST_TIMEOUT", 30),
        tnlcm_activate_timeout=_read_int(values, "TNLCM_ACTIVATE_TIMEOUT", 1800),
        tnlcm_activate_retry_delay=_read_int(values, "TNLCM_ACTIVATE_RETRY_DELAY", 5),
        tnlcm_activate_redeploy_max_attempts=_read_int(
            values,
            "TNLCM_ACTIVATE_REDEPLOY_MAX_ATTEMPTS",
            1,
        ),
        tnlcm_redeploy_delay=_read_int(values, "TNLCM_REDEPLOY_DELAY", 5),
        tnlcm_recovery_destroy_delay=_read_int(values, "TNLCM_RECOVERY_DESTROY_DELAY", 0),
        tnlcm_report_timeout=_read_int(values, "TNLCM_REPORT_TIMEOUT", 300),
        poll_interval=_read_int(values, "POLL_INTERVAL", 10),
        executions_file=_read_str(values, "EXECUTIONS_FILE", "./executions.json"),
        artifacts_dir=_read_str(values, "ARTIFACTS_DIR", "./artifacts"),
        examples_dir=_read_str(values, "EXAMPLES_DIR", "./examples"),
        log_level=_read_str(values, "LOG_LEVEL", "INFO").upper(),
    )


def _validate_settings(candidate: Settings) -> None:
    if not candidate.api_key:
        raise ValueError("API_KEY cannot be empty")
    if candidate.log_level not in ALLOWED_LOG_LEVELS:
        raise ValueError(f"LOG_LEVEL must be one of: {sorted(ALLOWED_LOG_LEVELS)}")
    if not candidate.tnlcm_url.startswith(("http://", "https://")):
        raise ValueError("TNLCM_URL must start with http:// or https://")
    if not candidate.elcm_url.startswith(("http://", "https://")):
        raise ValueError("ELCM_URL must start with http:// or https://")

    positive_fields = {
        "request_timeout": candidate.request_timeout,
        "tnlcm_activate_timeout": candidate.tnlcm_activate_timeout,
        "tnlcm_activate_retry_delay": candidate.tnlcm_activate_retry_delay,
        "tnlcm_report_timeout": candidate.tnlcm_report_timeout,
        "poll_interval": candidate.poll_interval,
    }
    for name, value in positive_fields.items():
        if value <= 0:
            raise ValueError(f"{name} must be greater than 0")

    non_negative_fields = {
        "tnlcm_activate_redeploy_max_attempts": candidate.tnlcm_activate_redeploy_max_attempts,
        "tnlcm_redeploy_delay": candidate.tnlcm_redeploy_delay,
        "tnlcm_recovery_destroy_delay": candidate.tnlcm_recovery_destroy_delay,
    }
    for name, value in non_negative_fields.items():
        if value < 0:
            raise ValueError(f"{name} must be >= 0")


_reload_lock = Lock()
settings = _load_settings()
_validate_settings(settings)


def reload_mutable_settings() -> dict[str, list[str]]:
    """Reload only mutable config fields from .env in-process."""
    with _reload_lock:
        env_values = dotenv_values(".env")
        candidate = _load_settings(env_values)
        _validate_settings(candidate)

        updated_fields: list[str] = []
        for field in sorted(RELOADABLE_SETTING_FIELDS):
            old_value = getattr(settings, field)
            new_value = getattr(candidate, field)
            if old_value == new_value:
                continue
            setattr(settings, field, new_value)
            updated_fields.append(field)

        return {
            "updated_fields": updated_fields,
            "non_reloadable_fields": sorted(NON_RELOADABLE_SETTING_FIELDS),
        }
