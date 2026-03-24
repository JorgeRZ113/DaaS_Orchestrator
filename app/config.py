from pydantic import BaseModel
from dotenv import load_dotenv
import os

load_dotenv()


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _env_url(name: str, default: str) -> str:
    # Normaliza para evitar dobles barras al concatenar rutas (/api/...)
    return _env_str(name, default).rstrip("/")


class Settings(BaseModel):
    app_env: str = _env_str("APP_ENV", "dev")
    app_host: str = _env_str("APP_HOST", "0.0.0.0")
    app_port: int = int(os.getenv("APP_PORT", "8000"))

    api_key: str = _env_str("API_KEY", "changeme")

    tnlcm_url: str = _env_url("TNLCM_URL", "http://localhost:5000")
    tnlcm_user: str = _env_str("TNLCM_USER", "")
    tnlcm_password: str = _env_str("TNLCM_PASSWORD", "")
    tnlcm_token: str = _env_str("TNLCM_TOKEN", "changeme")

    elcm_url: str = _env_url("ELCM_URL", "http://localhost:8080")

    request_timeout: int = int(os.getenv("REQUEST_TIMEOUT", "30"))
    tnlcm_activate_timeout: int = int(os.getenv("TNLCM_ACTIVATE_TIMEOUT", "1800"))
    tnlcm_activate_retry_delay: int = int(os.getenv("TNLCM_ACTIVATE_RETRY_DELAY", "5"))
    tnlcm_activate_redeploy_max_attempts: int = int(
        os.getenv("TNLCM_ACTIVATE_REDEPLOY_MAX_ATTEMPTS", "1")
    )
    tnlcm_redeploy_delay: int = int(os.getenv("TNLCM_REDEPLOY_DELAY", "5"))
    tnlcm_recovery_destroy_delay: int = int(os.getenv("TNLCM_RECOVERY_DESTROY_DELAY", "0"))
    tnlcm_report_timeout: int = int(os.getenv("TNLCM_REPORT_TIMEOUT", "300"))
    poll_interval: int = int(os.getenv("POLL_INTERVAL", "10"))

    executions_file: str = _env_str("EXECUTIONS_FILE", "./executions.json")
    artifacts_dir: str = _env_str("ARTIFACTS_DIR", "./artifacts")
    examples_dir: str = _env_str("EXAMPLES_DIR", "./examples")
    log_level: str = _env_str("LOG_LEVEL", "INFO")


settings = Settings()