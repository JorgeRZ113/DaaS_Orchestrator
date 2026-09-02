"""Tests de pausar y reconectar una TN: POST /pause, POST /resume y GET /executions.

Lo que se protege aqui es la promesa que motivo el estado PAUSED: apartar una TN
y volver a ella **sin redesplegar**, de modo que su descriptor original y su
historial de experimentos sobrevivan. Por eso ninguna de las dos rutas acepta
cuerpo y ninguna toca TNLCM mas alla de preguntarle el estado.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters import wireguard
from app.core.config import settings
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord
from app.main import app
from app.services import background, state

client = TestClient(app)


def _headers() -> dict[str, str]:
    # Leer la API key en el momento de la peticion: otros tests mutan settings.
    return {"x-api-key": settings.api_key}


@pytest.fixture(autouse=True)
def isolated_executions(monkeypatch):
    """Aisla el estado en memoria y evita disco y background tasks reales."""
    monkeypatch.setattr(state, "executions", {})
    monkeypatch.setattr(state, "save_to_disk", lambda: None)

    spawned: list[str] = []

    def _fake_spawn(coro, *, name: str):
        coro.close()
        spawned.append(name)
        return None

    monkeypatch.setattr(background, "spawn_background_task", _fake_spawn)
    yield spawned


@pytest.fixture
def conf_file(tmp_path):
    """Un `.conf` de WireGuard que existe de verdad: `ensure_tunnel_up` lo exige."""
    path = tmp_path / "tn-a.conf"
    path.write_text("[Interface]\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def tunnel_calls(monkeypatch):
    """Sustituye los dos subprocesos de WireGuard y anota como se llamaron."""
    calls: dict[str, list[tuple[str, str | None]]] = {"up": [], "down": []}

    monkeypatch.setattr(
        wireguard, "up_tunnel", lambda tn_id, conf_path: calls["up"].append((tn_id, conf_path))
    )
    monkeypatch.setattr(
        wireguard,
        "down_tunnel",
        lambda tn_id, conf_path=None: calls["down"].append((tn_id, conf_path)),
    )
    return calls


def _alive_tn(monkeypatch, tn_state: str = "activated") -> None:
    async def _fake_get_tn_state(tn_id, client=None):
        return tn_state

    monkeypatch.setattr("app.adapters.tnlcm.get_tn_state", _fake_get_tn_state)


def _add_record(execution_id: str, status: ExecutionState, **kwargs) -> ExecutionRecord:
    record = ExecutionRecord(execution_id=execution_id, status=status, **kwargs)
    state.executions[execution_id] = record
    return record


# ---------------------------------------------------------------------------
# POST /executions/{id}/pause
# ---------------------------------------------------------------------------


def test_pause_returns_404_when_execution_missing() -> None:
    response = client.post("/executions/missing/pause", headers=_headers())
    assert response.status_code == 404


def test_pause_returns_404_when_tn_id_missing() -> None:
    _add_record("tn-a", ExecutionState.tn_ready)

    response = client.post("/executions/tn-a/pause", headers=_headers())

    assert response.status_code == 404
    assert "tn_id missing" in response.json()["detail"]


def test_pause_returns_409_while_experiment_running(tunnel_calls) -> None:
    _add_record("tn-a", ExecutionState.running_experiment, tn_id="tn-a")

    response = client.post("/executions/tn-a/pause", headers=_headers())

    assert response.status_code == 409
    assert tunnel_calls["down"] == []


def test_pause_returns_409_when_already_paused(tunnel_calls) -> None:
    _add_record("tn-a", ExecutionState.paused, tn_id="tn-a")

    response = client.post("/executions/tn-a/pause", headers=_headers())

    assert response.status_code == 409
    assert "already paused" in response.json()["detail"]
    assert tunnel_calls["down"] == []


def test_pause_returns_409_from_a_state_that_is_not_ready(tunnel_calls) -> None:
    _add_record("tn-a", ExecutionState.deploying, tn_id="tn-a")

    response = client.post("/executions/tn-a/pause", headers=_headers())

    assert response.status_code == 409
    assert tunnel_calls["down"] == []


def test_pause_brings_the_tunnel_down_and_keeps_the_tn(tunnel_calls, conf_file) -> None:
    _add_record(
        "tn-a",
        ExecutionState.tn_ready,
        tn_id="tn-real-id",
        vpn_interface="tn-real-id",
        vpn_conf_path=conf_file,
        vpn_status="UP",
    )

    response = client.post("/executions/tn-a/pause", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PAUSED"
    assert body["vpn_status"] == "DOWN"
    # La TN sigue direccionable: pausar no la borra ni la olvida.
    assert body["tn_id"] == "tn-real-id"
    assert tunnel_calls["down"] == [("tn-real-id", conf_file)]


def test_pause_reports_207_when_the_tunnel_cannot_be_brought_down(monkeypatch, conf_file) -> None:
    """Aun asi queda PAUSED: dejarla en TN_READY la haria pasar por conectada."""

    def _fail(tn_id, conf_path=None):
        raise wireguard.WireGuardError("no such tunnel service")

    monkeypatch.setattr(wireguard, "down_tunnel", _fail)
    _add_record(
        "tn-a",
        ExecutionState.tn_ready,
        tn_id="tn-a",
        vpn_conf_path=conf_file,
        vpn_status="UP",
    )

    response = client.post("/executions/tn-a/pause", headers=_headers())

    assert response.status_code == 207
    body = response.json()
    assert body["status"] == "PAUSED"
    assert body["vpn_status"] == "DOWN_ERROR"
    assert state.executions["tn-a"].vpn_error


# ---------------------------------------------------------------------------
# POST /executions/{id}/resume
# ---------------------------------------------------------------------------


def test_resume_returns_404_when_execution_missing() -> None:
    response = client.post("/executions/missing/resume", headers=_headers())
    assert response.status_code == 404


def test_resume_returns_409_from_a_state_that_is_still_moving(tunnel_calls) -> None:
    """Un despliegue en curso no es algo a lo que reconectarse."""
    _add_record("tn-a", ExecutionState.deploying, tn_id="tn-a")

    response = client.post("/executions/tn-a/resume", headers=_headers())

    assert response.status_code == 409
    assert "DEPLOYING" in response.json()["detail"]
    assert tunnel_calls["up"] == []


def test_resume_reopens_the_tunnel_and_returns_to_tn_ready(
    monkeypatch, tunnel_calls, conf_file
) -> None:
    _alive_tn(monkeypatch)
    _add_record(
        "tn-a",
        ExecutionState.paused,
        tn_id="tn-real-id",
        vpn_interface="tn-real-id",
        vpn_conf_path=conf_file,
        vpn_status="DOWN",
    )

    response = client.post("/executions/tn-a/resume", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "TN_READY"
    assert body["vpn_status"] == "UP"
    assert tunnel_calls["up"] == [("tn-real-id", conf_file)]


def test_resume_keeps_the_descriptor_and_the_experiment_history(
    monkeypatch, tunnel_calls, conf_file
) -> None:
    """La razon de ser del endpoint: reconectar no regenera nada."""
    _alive_tn(monkeypatch)
    _add_record(
        "tn-a",
        ExecutionState.paused,
        tn_id="tn-a",
        vpn_conf_path=conf_file,
        vpn_status="DOWN",
        dataset_output=["csv"],
        artifacts=["artifacts/tn-a/dataset_descriptor.yaml"],
        experiments=[{"name": "exp-1", "status": "FINISHED"}],
    )

    response = client.post("/executions/tn-a/resume", headers=_headers())

    assert response.status_code == 200
    record = state.executions["tn-a"]
    assert record.artifacts == ["artifacts/tn-a/dataset_descriptor.yaml"]
    assert [run.name for run in record.experiments] == ["exp-1"]
    assert record.dataset_output == ["csv"]


def test_resume_is_idempotent_when_already_connected(monkeypatch, tunnel_calls) -> None:
    async def _should_not_run(tn_id, client=None):
        raise AssertionError("TNLCM should not be queried for an already connected TN")

    monkeypatch.setattr("app.adapters.tnlcm.get_tn_state", _should_not_run)
    _add_record("tn-a", ExecutionState.tn_ready, tn_id="tn-a", vpn_status="UP")

    response = client.post("/executions/tn-a/resume", headers=_headers())

    assert response.status_code == 200
    assert response.json()["status"] == "TN_READY"
    assert tunnel_calls["up"] == []


def test_resume_returns_409_when_another_tn_is_still_connected(
    monkeypatch, tunnel_calls, conf_file
) -> None:
    _alive_tn(monkeypatch)
    _add_record("tn-b", ExecutionState.tn_ready, tn_id="tn-b", vpn_status="UP")
    _add_record(
        "tn-a", ExecutionState.paused, tn_id="tn-a", vpn_conf_path=conf_file, vpn_status="DOWN"
    )

    response = client.post("/executions/tn-a/resume", headers=_headers())

    assert response.status_code == 409
    detail = response.json()["detail"]
    # El mensaje tiene que decir A QUIEN pausar, no solo que no se puede.
    assert "tn-b" in detail
    assert "/executions/tn-b/pause" in detail
    assert tunnel_calls["up"] == []


def test_resume_returns_409_when_the_tn_is_gone_in_tnlcm(
    monkeypatch, tunnel_calls, conf_file
) -> None:
    _alive_tn(monkeypatch, tn_state="purged")
    _add_record(
        "tn-a", ExecutionState.paused, tn_id="tn-a", vpn_conf_path=conf_file, vpn_status="DOWN"
    )

    response = client.post("/executions/tn-a/resume", headers=_headers())

    assert response.status_code == 409
    assert "POST /executions" in response.json()["detail"]
    assert tunnel_calls["up"] == []


def test_resume_goes_ahead_when_tnlcm_does_not_answer(monkeypatch, tunnel_calls, conf_file) -> None:
    """Reabrir el tunel no necesita a TNLCM y la reconexion se pidio explicitamente."""

    async def _raise_transport_error(tn_id, client=None):
        raise httpx.ConnectError("TNLCM unreachable")

    monkeypatch.setattr("app.adapters.tnlcm.get_tn_state", _raise_transport_error)
    _add_record(
        "tn-a", ExecutionState.paused, tn_id="tn-a", vpn_conf_path=conf_file, vpn_status="DOWN"
    )

    response = client.post("/executions/tn-a/resume", headers=_headers())

    assert response.status_code == 200
    assert response.json()["status"] == "TN_READY"
    assert "TNLCM did not answer" in response.json()["message"]
    assert tunnel_calls["up"] == [("tn-a", conf_file)]


def test_resume_reports_207_when_the_config_is_gone(monkeypatch, tunnel_calls, tmp_path) -> None:
    _alive_tn(monkeypatch)
    _add_record(
        "tn-a",
        ExecutionState.paused,
        tn_id="tn-a",
        vpn_conf_path=str(tmp_path / "desaparecido.conf"),
        vpn_status="DOWN",
    )

    response = client.post("/executions/tn-a/resume", headers=_headers())

    assert response.status_code == 207
    body = response.json()
    assert body["status"] == "TN_READY"
    assert body["vpn_status"] == "MANUAL_REQUIRED"
    assert tunnel_calls["up"] == []


# ---------------------------------------------------------------------------
# PAUSED frente al resto del ciclo de vida
# ---------------------------------------------------------------------------


def test_elcm_on_a_paused_tn_asks_for_an_explicit_resume(monkeypatch, isolated_executions) -> None:
    async def _should_not_run(tn_id, client=None):
        raise AssertionError("una TN pausada no se reconcilia sola")

    monkeypatch.setattr("app.adapters.tnlcm.get_tn_state", _should_not_run)
    _add_record("tn-a", ExecutionState.paused, tn_id="tn-a")
    body = {
        "experiment": {"name": "exp", "testcase_paths": ["TC_1_Preflight.yml"], "ues_paths": []}
    }

    response = client.post("/executions/tn-a/elcm?wait=false", json=body, headers=_headers())

    assert response.status_code == 409
    assert "/executions/tn-a/resume" in response.json()["detail"]
    assert isolated_executions == []


def test_a_paused_tn_can_still_be_removed(isolated_executions) -> None:
    """Sigue viva en TNLCM y ocupando recursos: no puede hacer falta reconectarla."""
    _add_record("tn-a", ExecutionState.paused, tn_id="tn-real-id")

    response = client.delete("/executions/tn-a/tn?wait=false", headers=_headers())

    assert response.status_code == 202
    assert response.json()["status"] == "DESTROYING"
    assert isolated_executions == ["teardown:tn-a"]


# ---------------------------------------------------------------------------
# GET /executions
# ---------------------------------------------------------------------------


def test_list_executions_is_empty_without_executions() -> None:
    response = client.get("/executions", headers=_headers())

    assert response.status_code == 200
    assert response.json() == []


def test_list_executions_shows_which_tn_holds_the_tunnel() -> None:
    _add_record("tn-a", ExecutionState.paused, tn_id="tn-a", vpn_status="DOWN")
    _add_record("tn-b", ExecutionState.tn_ready, tn_id="tn-b", vpn_status="UP")

    response = client.get("/executions", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    assert [item["execution_id"] for item in body] == ["tn-a", "tn-b"]
    assert {item["execution_id"]: item["vpn_status"] for item in body} == {
        "tn-a": "DOWN",
        "tn-b": "UP",
    }


def test_list_executions_requires_the_api_key() -> None:
    response = client.get("/executions", headers={"x-api-key": "bad-key"})
    assert response.status_code == 401
