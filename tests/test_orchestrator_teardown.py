"""Tests del bloque de borrado de la TN (run_teardown_phase / start_tn_teardown)."""

import pytest

from app import orchestrator
from app.config import settings
from app.models import ExecutionRecord, ExecutionState


@pytest.fixture(autouse=True)
def _isolate_orchestrator_state(monkeypatch, tmp_path):
    previous_artifacts_dir = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)
    previous_executions = orchestrator.executions.copy()

    orchestrator.executions.clear()

    monkeypatch.setattr(orchestrator, "_save_executions_to_disk", lambda: None)

    yield

    orchestrator.executions.clear()
    settings.artifacts_dir = previous_artifacts_dir
    orchestrator.executions.update(previous_executions)


def _tn_ready_record(execution_id: str) -> ExecutionRecord:
    record = ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.tn_ready,
        tn_id=execution_id,
        vpn_interface=execution_id,
        vpn_conf_path="C:/tmp/tn.conf",
        vpn_status="UP",
        message="TN ready",
    )
    orchestrator.executions[execution_id] = record
    return record


@pytest.mark.asyncio
async def test_teardown_downs_vpn_destroys_tn_and_marks_destroyed(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _fake_destroy_trial_network(tn_id: str) -> None:
        calls.append(("destroy", tn_id))

    def _fake_down_tunnel(tn_id: str, conf_path: str | None = None) -> None:
        calls.append(("down", tn_id))

    monkeypatch.setattr("app.tnlcm.destroy_trial_network", _fake_destroy_trial_network)
    monkeypatch.setattr("app.utils.wireguard.down_tunnel", _fake_down_tunnel)

    execution_id = "tn-teardown"
    _tn_ready_record(execution_id)

    await orchestrator.run_teardown_phase(execution_id)

    record = orchestrator.executions[execution_id]
    assert record.status == ExecutionState.destroyed
    assert "destroyed and purged" in record.message
    assert record.vpn_status == "DOWN"
    assert calls == [("down", execution_id), ("destroy", execution_id)]


@pytest.mark.asyncio
async def test_teardown_failure_leaves_failed_state_for_retry(monkeypatch):
    async def _fake_destroy_trial_network(tn_id: str) -> None:
        raise RuntimeError("TNLCM unreachable")

    def _fake_down_tunnel(tn_id: str, conf_path: str | None = None) -> None:
        return None

    monkeypatch.setattr("app.tnlcm.destroy_trial_network", _fake_destroy_trial_network)
    monkeypatch.setattr("app.utils.wireguard.down_tunnel", _fake_down_tunnel)

    execution_id = "tn-teardown-fail"
    _tn_ready_record(execution_id)

    await orchestrator.run_teardown_phase(execution_id)

    record = orchestrator.executions[execution_id]
    assert record.status == ExecutionState.failed
    assert "TNLCM unreachable" in (record.error or "")

    # Desde FAILED con tn_id el borrado se puede volver a lanzar
    spawned: list[str] = []

    def _fake_spawn(coro, *, name: str):
        coro.close()
        spawned.append(name)
        return None

    monkeypatch.setattr(orchestrator, "_spawn_background_task", _fake_spawn)
    retry_record = orchestrator.start_tn_teardown(execution_id)
    assert retry_record.status == ExecutionState.destroying
    assert spawned == [f"teardown:{execution_id}"]


@pytest.mark.asyncio
async def test_teardown_without_tn_is_a_noop(monkeypatch):
    async def _fail_destroy(tn_id: str) -> None:
        raise AssertionError("destroy_trial_network should not be called without tn_id")

    monkeypatch.setattr("app.tnlcm.destroy_trial_network", _fail_destroy)

    execution_id = "tn-no-tnid"
    orchestrator.executions[execution_id] = ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.failed,
        message="failed before deploy",
    )

    await orchestrator.run_teardown_phase(execution_id)

    assert orchestrator.executions[execution_id].status == ExecutionState.failed
