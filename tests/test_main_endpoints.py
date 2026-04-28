from fastapi.testclient import TestClient

from app.config import settings
from app.main import app, verify_api_key
from app.models import ExecutionRecord, ExecutionState
from app.orchestrator import TnlcmDeploymentInProgressError


client = TestClient(app)


def test_verify_api_key_accepts_valid_key() -> None:
    verify_api_key(settings.api_key)


def test_verify_api_key_rejects_invalid_key() -> None:
    try:
        verify_api_key("bad-key")
        assert False, "verify_api_key should reject invalid key"
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 401


def test_post_reload_config_returns_updated_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.main.reload_mutable_settings",
        lambda: {
            "updated_fields": ["log_level"],
            "non_reloadable_fields": ["app_port", "artifacts_dir"],
        },
    )

    response = client.post("/config/reload", headers={"x-api-key": settings.api_key})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "reloaded"
    assert body["updated_fields"] == ["log_level"]


def test_post_reload_config_returns_400_on_validation_error(monkeypatch) -> None:
    def _raise_value_error() -> dict[str, list[str]]:
        raise ValueError("LOG_LEVEL must be one of")

    monkeypatch.setattr("app.main.reload_mutable_settings", _raise_value_error)

    response = client.post("/config/reload", headers={"x-api-key": settings.api_key})

    assert response.status_code == 400
    assert "LOG_LEVEL" in response.json()["detail"]


def test_get_execution_status_returns_404_when_missing(monkeypatch) -> None:
    monkeypatch.setattr("app.main.get_execution", lambda execution_id: None)

    response = client.get("/executions/missing", headers={"x-api-key": settings.api_key})

    assert response.status_code == 404
    assert response.json()["detail"] == "Ejecucion no encontrada"


def test_get_execution_detail_returns_record(monkeypatch) -> None:
    record = ExecutionRecord(
        execution_id="tn-demo",
        status=ExecutionState.completed,
        message="done",
        tn_id="tn-demo",
    )
    monkeypatch.setattr("app.main.get_execution", lambda execution_id: record)

    response = client.get("/executions/tn-demo/detail", headers={"x-api-key": settings.api_key})

    assert response.status_code == 200
    body = response.json()
    assert body["execution_id"] == "tn-demo"
    assert body["status"] == "COMPLETED"


def test_post_execution_returns_409_when_tnlcm_deploy_is_busy(monkeypatch) -> None:
    async def _raise_busy(descriptor):
        raise TnlcmDeploymentInProgressError("deploy en curso")

    monkeypatch.setattr("app.main.create_tnlcm_execution", _raise_busy)

    payload = {
        "infrastructure": {"name": "tn-a"},
        "experiment": {"name": "exp-a", "testcase_paths": ["tc.yml"], "ues_paths": []},
        "dataset": {"output": "logs"},
    }
    response = client.post("/executions", json=payload, headers={"x-api-key": settings.api_key})

    assert response.status_code == 409
    assert "curso" in response.json()["detail"]


def test_post_execution_tnlcm_returns_409_when_tnlcm_deploy_is_busy(monkeypatch) -> None:
    async def _raise_busy(descriptor):
        raise TnlcmDeploymentInProgressError("deploy en curso")

    monkeypatch.setattr("app.main.create_tnlcm_execution", _raise_busy)

    payload = {
        "infrastructure": {"name": "tn-b"},
        "experiment": {"name": "exp-b", "testcase_paths": ["tc.yml"], "ues_paths": []},
        "dataset": {"output": "logs"},
    }
    response = client.post("/executions/tnlcm", json=payload, headers={"x-api-key": settings.api_key})

    assert response.status_code == 409
    assert "curso" in response.json()["detail"]


