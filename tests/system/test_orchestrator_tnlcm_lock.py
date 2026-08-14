import pytest

from app.core.config import settings
from app.services import state
from app.services import reporting
from app.domain.descriptor import DatasetDescriptor
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord
from app.services import errors
from app.services.phases import tnlcm as tnlcm_phase


def _descriptor(name: str) -> DatasetDescriptor:
    return DatasetDescriptor.model_validate(
        {
            "infrastructure": {"name": name},
            "experiment": {"name": "exp", "testcase_paths": ["tc.yml"], "ues_paths": []},
            "dataset": {"output": "logs"},
        }
    )


def test_tnlcm_deploy_slot_blocks_parallel_acquire() -> None:
    state._tnlcm_deploy_in_progress = None

    state.acquire_tnlcm_deploy_slot("tn-1")
    with pytest.raises(errors.TnlcmDeploymentInProgressError):
        state.acquire_tnlcm_deploy_slot("tn-2")

    state.release_tnlcm_deploy_slot("tn-1")
    state.acquire_tnlcm_deploy_slot("tn-2")
    state.release_tnlcm_deploy_slot("tn-2")


@pytest.mark.asyncio
async def test_run_tnlcm_phase_releases_lock_on_failure(monkeypatch, tmp_path) -> None:
    execution_id = "tn-lock-release"
    descriptor = _descriptor(execution_id)
    previous_artifacts_dir = settings.artifacts_dir

    settings.artifacts_dir = str(tmp_path)

    state.executions[execution_id] = ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.pending,
        message="created",
    )
    state._tnlcm_deploy_in_progress = execution_id

    async def _fail_deploy(_infra):
        raise RuntimeError("deploy failed")

    monkeypatch.setattr("app.services.state.save_to_disk", lambda: None)
    monkeypatch.setattr("app.adapters.tnlcm.deploy_trial_network", _fail_deploy)

    try:
        await tnlcm_phase.run_tnlcm_phase(execution_id, descriptor)

        assert state._tnlcm_deploy_in_progress is None
        assert state.executions[execution_id].status == ExecutionState.failed
    finally:
        settings.artifacts_dir = previous_artifacts_dir


@pytest.mark.asyncio
async def test_persist_telemetry_report_skips_when_flag_disabled(monkeypatch) -> None:
    previous = reporting.settings.telemetry_report_artifacts

    try:
        reporting.settings.telemetry_report_artifacts = False

        async def _should_not_run(*_args, **_kwargs):
            raise AssertionError("artifact builder should not be called when flag is disabled")

        monkeypatch.setattr(
            "app.storage.artifacts.build_telemetry_report_artifact", _should_not_run
        )

        result = await reporting.persist_telemetry_report_best_effort(
            "exec-disabled", "tnlcm_completed"
        )

        assert result is None
    finally:
        reporting.settings.telemetry_report_artifacts = previous
