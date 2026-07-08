from fastapi.testclient import TestClient

from app.config import settings
from app.main import app, verify_api_key
from app.models import ExecutionRecord, ExecutionState
from app.orchestrator import TnlcmDeploymentInProgressError
import app.tnlcm as tnlcm

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

    response = client.post("/refresh", headers={"x-api-key": settings.api_key})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "reloaded"
    assert body["updated_fields"] == ["log_level"]


def test_post_reload_config_returns_400_on_validation_error(monkeypatch) -> None:
    def _raise_value_error() -> dict[str, list[str]]:
        raise ValueError("LOG_LEVEL must be one of")

    monkeypatch.setattr("app.main.reload_mutable_settings", _raise_value_error)

    response = client.post("/refresh", headers={"x-api-key": settings.api_key})

    assert response.status_code == 400
    assert "LOG_LEVEL" in response.json()["detail"]


def test_post_register_stores_tokens_and_returns_preview(monkeypatch) -> None:
    previous_access = tnlcm._tnlcm_access_token
    previous_refresh = tnlcm._tnlcm_refresh_token

    class _FakeResponse:
        def __init__(self, payload: dict[str, str], status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return self._payload

    class _FakeAsyncClient:
        def __init__(self, timeout) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, **kwargs):
            if url.endswith("/api/v1/user/register"):
                return _FakeResponse({"message": "registered"})
            if url.endswith("/api/v1/user/login"):
                return _FakeResponse(
                    {
                        "access_token": "abcdefghijklmnopqrstuvwxyz123456",
                        "refresh_token": "refresh-token-xyz",
                    }
                )
            raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("app.main.httpx.AsyncClient", _FakeAsyncClient)

    try:
        response = client.post(
            "/register",
            params={
                "username": "jorge",
                "password": "secret",
                "email": "jorge@example.com",
                "org": "tfg",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["token_preview"].startswith("abcdefghijkl")
        assert body["token_preview"].endswith("123456")
        assert tnlcm._tnlcm_access_token == "abcdefghijklmnopqrstuvwxyz123456"
        assert tnlcm._tnlcm_refresh_token == "refresh-token-xyz"
    finally:
        tnlcm._tnlcm_access_token = previous_access
        tnlcm._tnlcm_refresh_token = previous_refresh


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


def test_post_execution_accepts_flat_component_template_fields(monkeypatch) -> None:
    async def _ok_record(descriptor):
        return ExecutionRecord(
            execution_id=descriptor.infrastructure.name,
            status=ExecutionState.pending,
            message="accepted",
        )

    monkeypatch.setattr("app.main.create_tnlcm_execution", _ok_record)

    payload = {
        "infrastructure": {
            "name": "tn-demo-test",
            "component": {
                "base": {
                    "influxdb_user": "admin",
                    "influxdb_password": "adminadmin",
                    "grafana_password": "adminadmin",
                }
            },
            "parameters": {
                "library_reference_type": "branch",
                "library_reference_value": "develop",
            },
        },
        "experiment": {"name": "exp-demo", "testcase_paths": ["TestCase_ping.yml"], "ues_paths": []},
        "dataset": {"output": "logs"},
        "auto_start_elcm": True,
    }

    response = client.post("/executions", json=payload, headers={"x-api-key": settings.api_key})

    assert response.status_code == 202
    body = response.json()
    assert body["execution_id"] == "tn-demo-test"


def test_post_execution_rejects_unknown_flat_component_field(monkeypatch) -> None:
    async def _ok_record(descriptor):
        return ExecutionRecord(
            execution_id=descriptor.infrastructure.name,
            status=ExecutionState.pending,
            message="accepted",
        )

    monkeypatch.setattr("app.main.create_tnlcm_execution", _ok_record)

    payload = {
        "infrastructure": {
            "name": "tn-demo-test",
            "component": {"base": {"influxdb_user": "admin", "unknown_field": "x"}},
            "parameters": {
                "library_reference_type": "branch",
                "library_reference_value": "develop",
            },
        },
        "experiment": {"name": "exp-demo", "testcase_paths": ["TestCase_ping.yml"], "ues_paths": []},
        "dataset": {"output": "logs"},
        "auto_start_elcm": True,
    }

    response = client.post("/executions", json=payload, headers={"x-api-key": settings.api_key})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert any("component.base.unknown_field" in item for item in detail["invalid_fields"])


def test_post_execution_accepts_flat_mongodb_fields_without_version(monkeypatch) -> None:
    async def _ok_record(descriptor):
        return ExecutionRecord(
            execution_id=descriptor.infrastructure.name,
            status=ExecutionState.pending,
            message="accepted",
        )

    monkeypatch.setattr("app.main.create_tnlcm_execution", _ok_record)

    payload = {
        "infrastructure": {
            "name": "tn-demo-mongo",
            "component": {
                "mongodb": {
                    "user": "mongo-user",
                    "password": "mongo-pass",
                }
            },
            "parameters": {
                "library_reference_type": "branch",
                "library_reference_value": "develop",
            },
        },
        "experiment": {"name": "exp-demo", "testcase_paths": ["TestCase_ping.yml"], "ues_paths": []},
        "dataset": {"output": "logs"},
        "auto_start_elcm": True,
    }

    response = client.post("/executions", json=payload, headers={"x-api-key": settings.api_key})

    assert response.status_code == 202
    body = response.json()
    assert body["execution_id"] == "tn-demo-mongo"


