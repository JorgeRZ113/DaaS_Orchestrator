import pytest

from app import orchestrator
from app.models import DatasetDescriptor, ExecutionRecord, ExecutionState


def _descriptor(name: str) -> DatasetDescriptor:
    return DatasetDescriptor.model_validate(
        {
            "infrastructure": {"name": name},
            "experiment": {"name": "exp", "testcase_paths": ["tc.yml"], "ues_paths": []},
            "dataset": {"output": "logs"},
        }
    )


def test_tnlcm_deploy_slot_blocks_parallel_acquire() -> None:
    orchestrator._tnlcm_deploy_in_progress = None

    orchestrator._acquire_tnlcm_deploy_slot("tn-1")
    with pytest.raises(orchestrator.TnlcmDeploymentInProgressError):
        orchestrator._acquire_tnlcm_deploy_slot("tn-2")

    orchestrator._release_tnlcm_deploy_slot("tn-1")
    orchestrator._acquire_tnlcm_deploy_slot("tn-2")
    orchestrator._release_tnlcm_deploy_slot("tn-2")


@pytest.mark.asyncio
async def test_run_tnlcm_phase_releases_lock_on_failure(monkeypatch) -> None:
    execution_id = "tn-lock-release"
    descriptor = _descriptor(execution_id)

    orchestrator.executions[execution_id] = ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.pending,
        message="created",
    )
    orchestrator._tnlcm_deploy_in_progress = execution_id

    async def _fail_deploy(_infra):
        raise RuntimeError("deploy failed")

    monkeypatch.setattr("app.orchestrator._save_executions_to_disk", lambda: None)
    monkeypatch.setattr("app.tnlcm.deploy_trial_network", _fail_deploy)

    await orchestrator.run_tnlcm_phase(execution_id, descriptor)

    assert orchestrator._tnlcm_deploy_in_progress is None
    assert orchestrator.executions[execution_id].status == ExecutionState.failed
