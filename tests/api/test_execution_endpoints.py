import httpx
from fastapi.testclient import TestClient

from app.core.config import settings
from app.api.deps import verify_api_key
from app.main import app
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord
from app.services.errors import TnlcmDeploymentInProgressError
import app.adapters.tnlcm as tnlcm

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
        "app.core.config.reload_mutable_settings",
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

    monkeypatch.setattr("app.core.config.reload_mutable_settings", _raise_value_error)

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

    monkeypatch.setattr("app.api.routers.auth.httpx.AsyncClient", _FakeAsyncClient)

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
    monkeypatch.setattr("app.services.orchestrator.get_execution", lambda execution_id: None)

    response = client.get("/executions/missing", headers={"x-api-key": settings.api_key})

    assert response.status_code == 404
    assert response.json()["detail"] == "Ejecucion no encontrada"


def test_get_execution_detail_returns_record_with_live_tn_state(monkeypatch) -> None:
    record = ExecutionRecord(
        execution_id="tn-demo",
        status=ExecutionState.completed,
        message="done",
        tn_id="tn-demo",
    )
    monkeypatch.setattr("app.services.orchestrator.get_execution", lambda execution_id: record)

    async def _fake_get_tn_state(tn_id, client=None):
        assert tn_id == "tn-demo"
        return "activated"

    monkeypatch.setattr("app.adapters.tnlcm.get_tn_state", _fake_get_tn_state)

    response = client.get("/executions/tn-demo/detail", headers={"x-api-key": settings.api_key})

    assert response.status_code == 200
    body = response.json()
    assert body["execution_id"] == "tn-demo"
    assert body["status"] == "COMPLETED"
    assert body["tn_state"] == "activated"


def test_get_execution_detail_leaves_tn_state_null_when_tnlcm_fails(monkeypatch) -> None:
    """Un fallo consultando TNLCM no puede tumbar la consulta del detalle."""
    record = ExecutionRecord(
        execution_id="tn-demo",
        status=ExecutionState.completed,
        message="done",
        tn_id="tn-demo",
    )
    monkeypatch.setattr("app.services.orchestrator.get_execution", lambda execution_id: record)

    async def _raise_transport_error(tn_id, client=None):
        raise httpx.ConnectError("TNLCM unreachable")

    monkeypatch.setattr("app.adapters.tnlcm.get_tn_state", _raise_transport_error)

    response = client.get("/executions/tn-demo/detail", headers={"x-api-key": settings.api_key})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["tn_state"] is None


def test_get_execution_detail_skips_tnlcm_when_there_is_no_tn(monkeypatch) -> None:
    record = ExecutionRecord(
        execution_id="tn-demo",
        status=ExecutionState.pending,
        message="Execution created",
    )
    monkeypatch.setattr("app.services.orchestrator.get_execution", lambda execution_id: record)

    async def _should_not_run(tn_id, client=None):
        raise AssertionError("TNLCM should not be queried without a tn_id")

    monkeypatch.setattr("app.adapters.tnlcm.get_tn_state", _should_not_run)

    response = client.get("/executions/tn-demo/detail", headers={"x-api-key": settings.api_key})

    assert response.status_code == 200
    assert response.json()["tn_state"] is None


def test_post_execution_returns_409_when_tnlcm_deploy_is_busy(monkeypatch) -> None:
    async def _raise_busy(descriptor, source=None):
        raise TnlcmDeploymentInProgressError("deploy en curso")

    monkeypatch.setattr("app.services.orchestrator.create_tnlcm_execution", _raise_busy)

    payload = {
        "infrastructure": {"name": "tn-a"},
        "experiment": {"name": "exp-a", "testcase_paths": ["TC_1_Preflight.yml"], "ues_paths": []},
        "dataset": {"output": "logs"},
    }
    response = client.post(
        "/executions?wait=false", json=payload, headers={"x-api-key": settings.api_key}
    )

    assert response.status_code == 409
    assert "curso" in response.json()["detail"]


def test_post_execution_accepts_flat_component_template_fields(monkeypatch) -> None:
    async def _ok_record(descriptor, source=None):
        return ExecutionRecord(
            execution_id=descriptor.infrastructure.name,
            status=ExecutionState.pending,
            message="accepted",
        )

    monkeypatch.setattr("app.services.orchestrator.create_tnlcm_execution", _ok_record)

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
        "experiment": {
            "name": "exp-demo",
            "testcase_paths": ["TC_1_Preflight.yml"],
            "ues_paths": [],
        },
        "dataset": {"output": "logs"},
        "auto_start_elcm": True,
    }

    response = client.post(
        "/executions?wait=false", json=payload, headers={"x-api-key": settings.api_key}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["execution_id"] == "tn-demo-test"


def test_post_execution_rejects_unknown_flat_component_field(monkeypatch) -> None:
    async def _ok_record(descriptor, source=None):
        return ExecutionRecord(
            execution_id=descriptor.infrastructure.name,
            status=ExecutionState.pending,
            message="accepted",
        )

    monkeypatch.setattr("app.services.orchestrator.create_tnlcm_execution", _ok_record)

    payload = {
        "infrastructure": {
            "name": "tn-demo-test",
            "component": {"base": {"influxdb_user": "admin", "unknown_field": "x"}},
            "parameters": {
                "library_reference_type": "branch",
                "library_reference_value": "develop",
            },
        },
        "experiment": {
            "name": "exp-demo",
            "testcase_paths": ["TC_1_Preflight.yml"],
            "ues_paths": [],
        },
        "dataset": {"output": "logs"},
        "auto_start_elcm": True,
    }

    response = client.post(
        "/executions?wait=false", json=payload, headers={"x-api-key": settings.api_key}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert any("component.base.unknown_field" in item for item in detail["invalid_fields"])


def test_post_execution_accepts_flat_mongodb_fields_without_version(monkeypatch) -> None:
    async def _ok_record(descriptor, source=None):
        return ExecutionRecord(
            execution_id=descriptor.infrastructure.name,
            status=ExecutionState.pending,
            message="accepted",
        )

    monkeypatch.setattr("app.services.orchestrator.create_tnlcm_execution", _ok_record)

    payload = {
        "infrastructure": {
            "name": "tn-demo-mongo",
            "component": {
                "mongodb": {
                    "user": "mongo-user",
                    "password": "mongo-pass",
                    "database": "mongo-db",
                    "express_user": "express-user",
                    "express_password": "express-pass",
                }
            },
            "parameters": {
                "library_reference_type": "branch",
                "library_reference_value": "develop",
            },
        },
        "experiment": {
            "name": "exp-demo",
            "testcase_paths": ["TC_1_Preflight.yml"],
            "ues_paths": [],
        },
        "dataset": {"output": "logs"},
        "auto_start_elcm": True,
    }

    response = client.post(
        "/executions?wait=false", json=payload, headers={"x-api-key": settings.api_key}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["execution_id"] == "tn-demo-mongo"


def test_post_execution_rejects_mongodb_without_required_credentials(monkeypatch) -> None:
    # Las credenciales de mongodb son obligatorias: debe cortar con 400 en el POST
    # en vez de aceptar y fallar luego al generar el descriptor.
    async def _ok_record(descriptor, source=None):
        return ExecutionRecord(
            execution_id=descriptor.infrastructure.name,
            status=ExecutionState.pending,
            message="accepted",
        )

    monkeypatch.setattr("app.services.orchestrator.create_tnlcm_execution", _ok_record)

    payload = {
        "infrastructure": {
            "name": "tn-demo-mongo",
            "component": {"mongodb": {"user": "mongo-user"}},
            "parameters": {
                "library_reference_type": "branch",
                "library_reference_value": "develop",
            },
        },
        "experiment": {
            "name": "exp-demo",
            "testcase_paths": ["TC_1_Preflight.yml"],
            "ues_paths": [],
        },
        "dataset": {"output": "logs"},
        "auto_start_elcm": True,
    }

    response = client.post(
        "/executions?wait=false", json=payload, headers={"x-api-key": settings.api_key}
    )

    assert response.status_code == 400
    invalid_fields = response.json()["detail"]["invalid_fields"]
    assert any(
        "component.mongodb.password: required field missing" == item for item in invalid_fields
    )
    assert any(
        "component.mongodb.database: required field missing" == item for item in invalid_fields
    )


# ---------------------------------------------------------------------------
# Rechazo de strings vacíos ("") en el body de POST /executions
# ---------------------------------------------------------------------------


def test_post_execution_rejects_empty_string_in_component_field(monkeypatch) -> None:
    called = {"create": False}

    async def _should_not_run(descriptor, source=None):  # pragma: no cover - no debe llamarse
        called["create"] = True
        return None

    monkeypatch.setattr("app.services.orchestrator.create_tnlcm_execution", _should_not_run)

    payload = {
        "infrastructure": {
            "name": "tn-demo-empty",
            "component": {
                "base": {
                    "influxdb_user": "admin",
                    "influxdb_password": "adminadmin",
                    "grafana_password": "",  # vacío: debe rechazar
                }
            },
        },
        "experiment": {
            "name": "exp-demo",
            "testcase_paths": ["TC_1_Preflight.yml"],
            "ues_paths": [],
        },
        "dataset": {"output": "logs"},
        "auto_start_elcm": True,
    }

    response = client.post(
        "/executions?wait=false", json=payload, headers={"x-api-key": settings.api_key}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["empty_fields"] == ["infrastructure.component.base.grafana_password"]
    assert "vac" in detail["message"].lower()
    # El gate corta antes de crear la ejecución.
    assert called["create"] is False


def test_post_execution_rejects_whitespace_only_and_reports_all_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.orchestrator.create_tnlcm_execution", lambda descriptor, source=None: None
    )

    payload = {
        "infrastructure": {
            "name": "tn-demo-multi",
            "descriptor_path": "   ",  # solo espacios: cuenta como vacío
            "component": {"base": {"influxdb_user": ""}},
        },
        "experiment": {
            "name": "exp-demo",
            "testcase_paths": ["", "TC_1_Preflight.yml"],  # vacío dentro de lista
            "ues_paths": [],
        },
        "dataset": {"output": "logs"},
    }

    response = client.post(
        "/executions?wait=false", json=payload, headers={"x-api-key": settings.api_key}
    )

    assert response.status_code == 400
    empty_fields = response.json()["detail"]["empty_fields"]
    assert empty_fields == [
        "experiment.testcase_paths[0]",
        "infrastructure.component.base.influxdb_user",
        "infrastructure.descriptor_path",
    ]


def test_post_execution_does_not_flag_empty_strings_from_server_defaults(monkeypatch) -> None:
    # Body válido y mínimo: los defaults del servidor (p.ej. message="") no deben
    # marcarse como vacíos porque solo inspeccionamos lo que envió el cliente.
    async def _ok_record(descriptor, source=None):
        return ExecutionRecord(
            execution_id=descriptor.infrastructure.name,
            status=ExecutionState.pending,
            message="accepted",
        )

    monkeypatch.setattr("app.services.orchestrator.create_tnlcm_execution", _ok_record)

    payload = {
        "infrastructure": {
            "name": "tn-demo-clean",
            "component": {
                "base": {
                    "influxdb_user": "admin",
                    "influxdb_password": "adminadmin",
                    "grafana_password": "adminadmin",
                }
            },
        },
        "experiment": {
            "name": "exp-demo",
            "testcase_paths": ["TC_1_Preflight.yml"],
            "ues_paths": [],
        },
        "dataset": {"output": "logs"},
    }

    response = client.post(
        "/executions?wait=false", json=payload, headers={"x-api-key": settings.api_key}
    )

    assert response.status_code == 202


def test_collect_empty_string_paths_walks_nested_structures() -> None:
    from app.api.validation import collect_empty_string_paths

    data = {
        "a": "value",
        "b": "",
        "c": {"d": "  ", "e": "ok"},
        "f": ["x", "", {"g": ""}],
    }

    assert collect_empty_string_paths(data) == ["b", "c.d", "f[1]", "f[2].g"]
    assert collect_empty_string_paths({"all": "good"}) == []
    assert collect_empty_string_paths("") == ["<root>"]
