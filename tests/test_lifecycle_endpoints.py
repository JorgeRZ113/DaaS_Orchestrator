"""Tests del ciclo de vida persistente de la TN: /elcm multi-experimento y borrado manual."""

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app.config import settings
from app.main import app
from app.models import (
    DatasetDescriptor,
    ExecutionRecord,
    ExecutionState,
    ExperimentRun,
)

client = TestClient(app)


def _headers() -> dict[str, str]:
    # Leer la API key en el momento de la peticion: otros tests mutan settings.
    return {"x-api-key": settings.api_key}


ELCM_BODY = {
    "experiment": {
        "name": "exp-demo",
        "testcase_paths": ["TestCase_ping.yml"],
        "ues_paths": [],
    }
}


@pytest.fixture(autouse=True)
def isolated_executions(monkeypatch):
    """Aisla el estado en memoria y evita disco y background tasks reales."""
    monkeypatch.setattr(orchestrator, "executions", {})
    monkeypatch.setattr(orchestrator, "_save_executions_to_disk", lambda: None)
    monkeypatch.setattr(orchestrator, "_tnlcm_deploy_in_progress", None)

    spawned: list[str] = []

    def _fake_spawn(coro, *, name: str):
        coro.close()
        spawned.append(name)
        return None

    monkeypatch.setattr(orchestrator, "_spawn_background_task", _fake_spawn)
    yield spawned


def _add_record(execution_id: str, status: ExecutionState, **kwargs) -> ExecutionRecord:
    record = ExecutionRecord(execution_id=execution_id, status=status, **kwargs)
    orchestrator.executions[execution_id] = record
    return record


# ---------------------------------------------------------------------------
# POST /executions/{id}/elcm
# ---------------------------------------------------------------------------


def test_elcm_returns_404_when_execution_missing() -> None:
    response = client.post("/executions/missing/elcm", json=ELCM_BODY, headers=_headers())
    assert response.status_code == 404


def test_elcm_returns_409_while_experiment_running(isolated_executions) -> None:
    _add_record("tn-a", ExecutionState.running_experiment, tn_id="tn-a")

    response = client.post("/executions/tn-a/elcm", json=ELCM_BODY, headers=_headers())

    assert response.status_code == 409
    assert "already running" in response.json()["detail"]
    assert isolated_executions == []


def test_elcm_returns_409_when_tn_not_ready(isolated_executions) -> None:
    _add_record("tn-a", ExecutionState.deploying)

    response = client.post("/executions/tn-a/elcm", json=ELCM_BODY, headers=_headers())

    assert response.status_code == 409
    assert isolated_executions == []


def test_elcm_accepts_experiment_on_ready_tn(isolated_executions) -> None:
    _add_record("tn-a", ExecutionState.tn_ready, tn_id="tn-a")

    response = client.post("/executions/tn-a/elcm", json=ELCM_BODY, headers=_headers())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "RUNNING_EXPERIMENT"
    assert body["tn_id"] == "tn-a"

    record = orchestrator.executions["tn-a"]
    assert [run.name for run in record.experiments] == ["exp-demo"]
    assert isolated_executions == ["elcm:tn-a:exp-demo"]


def test_elcm_rejects_duplicate_experiment_name(isolated_executions) -> None:
    _add_record(
        "tn-a",
        ExecutionState.tn_ready,
        tn_id="tn-a",
        experiments=[ExperimentRun(name="exp-demo", status="FINISHED")],
    )

    response = client.post("/executions/tn-a/elcm", json=ELCM_BODY, headers=_headers())

    assert response.status_code == 409
    assert "unique name" in response.json()["detail"]
    assert isolated_executions == []


def test_elcm_allows_second_experiment_with_new_name(isolated_executions) -> None:
    _add_record(
        "tn-a",
        ExecutionState.tn_ready,
        tn_id="tn-a",
        experiments=[ExperimentRun(name="exp-demo", status="FINISHED")],
    )
    body = {"experiment": {"name": "exp-demo-2", "testcase_paths": ["tc.yml"], "ues_paths": []}}

    response = client.post("/executions/tn-a/elcm", json=body, headers=_headers())

    assert response.status_code == 202
    record = orchestrator.executions["tn-a"]
    assert [run.name for run in record.experiments] == ["exp-demo", "exp-demo-2"]


# ---------------------------------------------------------------------------
# DELETE /executions/{id}/tn
# ---------------------------------------------------------------------------


def test_remove_tn_returns_404_when_execution_missing() -> None:
    response = client.delete("/executions/missing/tn", headers=_headers())
    assert response.status_code == 404


def test_remove_tn_returns_404_when_tn_id_missing(isolated_executions) -> None:
    _add_record("tn-a", ExecutionState.failed)

    response = client.delete("/executions/tn-a/tn", headers=_headers())

    assert response.status_code == 404
    assert "tn_id missing" in response.json()["detail"]


def test_remove_tn_returns_409_while_experiment_running(isolated_executions) -> None:
    _add_record("tn-a", ExecutionState.running_experiment, tn_id="tn-a")

    response = client.delete("/executions/tn-a/tn", headers=_headers())

    assert response.status_code == 409
    assert isolated_executions == []


def test_remove_tn_returns_409_when_already_destroyed(isolated_executions) -> None:
    _add_record("tn-a", ExecutionState.destroyed, tn_id="tn-a")

    response = client.delete("/executions/tn-a/tn", headers=_headers())

    assert response.status_code == 409
    assert isolated_executions == []


def test_remove_tn_triggers_teardown_and_reports_tn_id(isolated_executions) -> None:
    _add_record("tn-a", ExecutionState.tn_ready, tn_id="tn-real-id")

    response = client.delete("/executions/tn-a/tn", headers=_headers())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "DESTROYING"
    assert body["tn_id"] == "tn-real-id"
    assert "tn-real-id" in body["message"]
    assert isolated_executions == ["teardown:tn-a"]


def test_remove_tn_allowed_from_failed_state_with_tn(isolated_executions) -> None:
    _add_record("tn-a", ExecutionState.failed, tn_id="tn-real-id")

    response = client.delete("/executions/tn-a/tn", headers=_headers())

    assert response.status_code == 202
    assert isolated_executions == ["teardown:tn-a"]


# ---------------------------------------------------------------------------
# Modelos: matriz auto_start_elcm / ephemeral_tn
# ---------------------------------------------------------------------------


def test_descriptor_requires_experiment_when_auto_start() -> None:
    with pytest.raises(ValueError, match="experiment is required"):
        DatasetDescriptor(
            infrastructure={"name": "tn-a"},
            auto_start_elcm=True,
        )


def test_descriptor_allows_missing_experiment_when_manual() -> None:
    descriptor = DatasetDescriptor(
        infrastructure={"name": "tn-a"},
        auto_start_elcm=False,
    )
    assert descriptor.experiment is None
    assert descriptor.ephemeral_tn is False


@pytest.mark.asyncio
async def test_ephemeral_tn_is_ignored_when_manual_start(monkeypatch) -> None:
    """Con auto_start_elcm=False el flag ephemeral_tn no debe activarse en el record."""
    monkeypatch.setattr(
        "app.artifacts.persist_dataset_descriptor", lambda execution_id, descriptor: "desc.json"
    )
    descriptor = DatasetDescriptor(
        infrastructure={"name": "tn-manual"},
        auto_start_elcm=False,
        ephemeral_tn=True,
    )

    record = await orchestrator.create_tnlcm_execution(descriptor)

    assert record.ephemeral_tn is False


@pytest.mark.asyncio
async def test_ephemeral_tn_kept_when_auto_start(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.artifacts.persist_dataset_descriptor", lambda execution_id, descriptor: "desc.json"
    )
    descriptor = DatasetDescriptor(
        infrastructure={"name": "tn-auto"},
        experiment={"name": "exp-demo", "testcase_paths": ["tc.yml"], "ues_paths": []},
        auto_start_elcm=True,
        ephemeral_tn=True,
    )

    record = await orchestrator.create_tnlcm_execution(descriptor)

    assert record.ephemeral_tn is True
