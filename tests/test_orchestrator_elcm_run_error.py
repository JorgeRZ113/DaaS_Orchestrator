import pytest

from app import elcm
from app import orchestrator
from app.config import settings
from app.models import (
    DatasetDescriptor,
    ExecutionRecord,
    ExecutionState,
    ExperimentConfig,
    InfrastructureConfig,
)


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


@pytest.mark.asyncio
async def test_run_elcm_phase_run_error_is_propagated_without_retry(monkeypatch, caplog):
    execution_id = "tn-elcm-error"
    call_count = {"run_experiment": 0}

    async def _fake_upload_test_cases(testcase_paths, elcm_base_url=None, execution_id=None):
        return None

    async def _fake_run_experiment(experiment, elcm_base_url=None, execution_id=None):
        call_count["run_experiment"] += 1
        raise RuntimeError(
            "ELCM /experiment/run (HTTP 400): descriptor invalido. "
            "Corrija lo indicado por el error antes de volver a ejecutar la parte de ELCM."
        )

    async def _fake_destroy_trial_network(tn_id: str):
        return None

    def _fake_down_tunnel(vpn_interface: str, vpn_conf_path: str | None = None):
        return None

    monkeypatch.setattr("app.elcm.upload_test_cases", _fake_upload_test_cases)
    monkeypatch.setattr("app.elcm.run_experiment", _fake_run_experiment)
    monkeypatch.setattr("app.tnlcm.destroy_trial_network", _fake_destroy_trial_network)
    monkeypatch.setattr("app.utils.wireguard.down_tunnel", _fake_down_tunnel)
    caplog.set_level("INFO")

    descriptor = DatasetDescriptor(
        infrastructure=InfrastructureConfig(
            name=execution_id, descriptor_path="examples/tn_descriptor_elcm.yaml"
        ),
        experiment=ExperimentConfig(name="exp-elcm", testcase_paths=["examples/TestCase_ping.yml"]),
    )

    orchestrator.executions[execution_id] = ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.tn_ready,
        tn_id=execution_id,
        elcm_base_url="http://192.168.199.3:5000",
        vpn_interface=execution_id,
        vpn_status="UP",
        message="TN deployment completed",
    )

    await orchestrator.run_elcm_phase(execution_id, descriptor.experiment)

    record = orchestrator.executions[execution_id]
    assert call_count["run_experiment"] == 1
    # La TN sigue viva tras un experimento fallido: vuelve a TN_READY con el error registrado
    assert record.status == ExecutionState.tn_ready
    assert "descriptor invalido" in (record.error or "")
    assert "Corrija lo indicado por el error" in (record.error or "")
    assert "failed" in record.message
    assert "ELCM phase finalization completed" in caplog.text


@pytest.mark.asyncio
async def test_run_elcm_phase_upload_error_stops_before_run_experiment(monkeypatch):
    execution_id = "tn-upload-error"
    call_count = {"upload_test_cases": 0, "run_experiment": 0}

    async def _fake_upload_test_cases(testcase_paths, elcm_base_url=None, execution_id=None):
        call_count["upload_test_cases"] += 1
        raise elcm.TnUploadTestCaseError(
            "ELCM upload_test_case failed for TestCase_ping.yml (HTTP 400). Backend error: user_id ausente/invalido. Corrija lo indicado por el mensaje de error antes de volver a lanzar la parte de ELCM."
        )

    async def _fake_run_experiment(experiment, elcm_base_url=None, execution_id=None):
        call_count["run_experiment"] += 1
        return "exp-never-called"

    async def _fake_destroy_trial_network(tn_id: str):
        return None

    def _fake_down_tunnel(vpn_interface: str, vpn_conf_path: str | None = None):
        return None

    monkeypatch.setattr("app.elcm.upload_test_cases", _fake_upload_test_cases)
    monkeypatch.setattr("app.elcm.run_experiment", _fake_run_experiment)
    monkeypatch.setattr("app.tnlcm.destroy_trial_network", _fake_destroy_trial_network)
    monkeypatch.setattr("app.utils.wireguard.down_tunnel", _fake_down_tunnel)

    descriptor = DatasetDescriptor(
        infrastructure=InfrastructureConfig(
            name=execution_id, descriptor_path="examples/tn_descriptor_elcm.yaml"
        ),
        experiment=ExperimentConfig(name="exp-elcm", testcase_paths=["examples/TestCase_ping.yml"]),
    )

    orchestrator.executions[execution_id] = ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.tn_ready,
        tn_id=execution_id,
        elcm_base_url="http://192.168.199.3:5000",
        vpn_interface=execution_id,
        vpn_status="UP",
        message="TN deployment completed",
    )

    await orchestrator.run_elcm_phase(execution_id, descriptor.experiment)

    record = orchestrator.executions[execution_id]
    assert call_count["upload_test_cases"] == 1
    assert call_count["run_experiment"] == 0
    assert record.status == ExecutionState.tn_ready
    assert "user_id ausente/invalido" in (record.error or "")
    assert "Corrija lo indicado por el mensaje de error" in (record.error or "")


@pytest.mark.asyncio
async def test_run_elcm_phase_logs_not_found_bypasses_tn_status_check(monkeypatch):
    execution_id = "tn-elcm-not-found"
    call_count = {"get_tn_status": 0}

    async def _fake_upload_test_cases(testcase_paths, elcm_base_url=None, execution_id=None):
        return None

    async def _fake_run_experiment(experiment, elcm_base_url=None, execution_id=None):
        return "exp-404"

    async def _fake_get_experiment_status(experiment_id, elcm_base_url=None, execution_id=None):
        return "Finished"

    async def _fake_collect_results(experiment_id, elcm_base_url=None, execution_id=None):
        raise elcm.TnLogsNotFoundError(
            "ELCM reports execution exp-404 as not found in logs. El experimento no se ha podido hacer y hay que repetirlo."
        )

    async def _fake_build_artifacts(execution_id: str, tn_id: str, elcm_execution_id: str, results):
        return ["artifact-1"]

    async def _fake_destroy_trial_network(tn_id: str):
        return None

    def _fake_down_tunnel(vpn_interface: str, vpn_conf_path: str | None = None):
        return None

    def _fake_get_tn_status(tn_id: str):
        call_count["get_tn_status"] += 1
        return "ACTIVE"

    monkeypatch.setattr("app.elcm.upload_test_cases", _fake_upload_test_cases)
    monkeypatch.setattr("app.elcm.run_experiment", _fake_run_experiment)
    monkeypatch.setattr("app.elcm.get_experiment_status", _fake_get_experiment_status)
    monkeypatch.setattr("app.elcm.collect_results", _fake_collect_results)
    monkeypatch.setattr("app.tnlcm.destroy_trial_network", _fake_destroy_trial_network)
    monkeypatch.setattr("app.tnlcm.get_tn_status", _fake_get_tn_status)
    monkeypatch.setattr("app.artifacts.build_artifacts", _fake_build_artifacts)
    monkeypatch.setattr("app.utils.wireguard.down_tunnel", _fake_down_tunnel)

    descriptor = DatasetDescriptor(
        infrastructure=InfrastructureConfig(
            name=execution_id, descriptor_path="examples/tn_descriptor_elcm.yaml"
        ),
        experiment=ExperimentConfig(name="exp-elcm", testcase_paths=["examples/TestCase_ping.yml"]),
    )

    orchestrator.executions[execution_id] = ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.tn_ready,
        tn_id=execution_id,
        elcm_base_url="http://192.168.199.3:5000",
        vpn_interface=execution_id,
        vpn_status="UP",
        message="TN deployment completed",
    )

    await orchestrator.run_elcm_phase(execution_id, descriptor.experiment)

    record = orchestrator.executions[execution_id]
    assert call_count["get_tn_status"] == 0
    assert record.status == ExecutionState.tn_ready
    assert "not found" in (record.error or "").lower()
    assert "hay que repetirlo" in (record.error or "").lower()
