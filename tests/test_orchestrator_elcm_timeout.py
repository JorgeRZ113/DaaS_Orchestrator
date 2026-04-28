import pytest

from app.models import (
    DatasetDescriptor,
    ExecutionRecord,
    ExecutionState,
    ExperimentConfig,
    InfrastructureConfig,
)
from app import orchestrator


@pytest.fixture(autouse=True)
def _isolate_orchestrator_state(monkeypatch):
    previous_executions = orchestrator.executions.copy()
    previous_descriptors = orchestrator.execution_descriptors.copy()
    previous_watchdogs = orchestrator.elcm_start_watchdogs.copy()

    orchestrator.executions.clear()
    orchestrator.execution_descriptors.clear()
    orchestrator.elcm_start_watchdogs.clear()

    # Keep tests pure and avoid touching the real executions.json on every _update.
    monkeypatch.setattr(orchestrator, "_save_executions_to_disk", lambda: None)

    yield

    for task in orchestrator.elcm_start_watchdogs.values():
        task.cancel()

    orchestrator.executions.clear()
    orchestrator.execution_descriptors.clear()
    orchestrator.elcm_start_watchdogs.clear()

    orchestrator.executions.update(previous_executions)
    orchestrator.execution_descriptors.update(previous_descriptors)
    orchestrator.elcm_start_watchdogs.update(previous_watchdogs)


@pytest.mark.asyncio
async def test_elcm_timeout_cancels_and_cleans_up(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _fake_destroy_trial_network(tn_id: str) -> None:
        calls.append(("destroy", tn_id))

    def _fake_down_tunnel(tn_id: str, conf_path: str | None = None) -> None:
        calls.append(("down", tn_id))

    monkeypatch.setattr("app.tnlcm.destroy_trial_network", _fake_destroy_trial_network)
    monkeypatch.setattr("app.utils.wireguard.down_tunnel", _fake_down_tunnel)

    execution_id = "tn-timeout"
    orchestrator.executions[execution_id] = ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.completed,
        tn_id="tn-timeout",
        vpn_interface="tn-timeout",
        vpn_conf_path="C:/tmp/tn-timeout.conf",
        vpn_status="UP",
        message="TN deployment completed",
    )

    await orchestrator._handle_elcm_start_timeout(execution_id, timeout_seconds=300)

    record = orchestrator.executions[execution_id]
    assert record.status == ExecutionState.cancelled
    assert "destroyed/purged" in record.message
    assert "300 seconds" in (record.error or "")
    assert ("down", "tn-timeout") in calls
    assert ("destroy", "tn-timeout") in calls


@pytest.mark.asyncio
async def test_start_elcm_requires_completed_state():
    execution_id = "tn-not-ready"
    descriptor = DatasetDescriptor(
        infrastructure=InfrastructureConfig(
            name=execution_id,
            descriptor_path="examples/tn_descriptor_elcm.yaml",
        ),
        experiment=ExperimentConfig(
            name="exp-1",
            testcase_paths=["examples/TestCase_ping.yml"],
        ),
    )

    orchestrator.executions[execution_id] = ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.deploying,
        tn_id=execution_id,
        message="Deploying",
    )
    orchestrator.execution_descriptors[execution_id] = descriptor

    with pytest.raises(ValueError, match="COMPLETED"):
        await orchestrator.start_elcm_phase(execution_id)
