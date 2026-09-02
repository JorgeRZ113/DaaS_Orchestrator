"""Tests del cleanup consciente del estado en run_tnlcm_phase.

Cuando la fase TNLCM falla pero TNLCM reporta la TN como sana ('created' /
'activated'), la TN no debe destruirse: se conserva para poder reconciliarla con
un re-POST. Solo se destruye si el estado es terminal/parcial o ya no existe.
"""

import pytest

from app.services import state
from app.domain.descriptor import DatasetDescriptor, InfrastructureConfig
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord
from app.services.phases import tnlcm as tnlcm_phase

pytestmark = pytest.mark.usefixtures("isolate_orchestrator_state")


def _pending_record(execution_id: str) -> ExecutionRecord:
    record = ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.pending,
        message="Execution created",
    )
    state.executions[execution_id] = record
    return record


def _descriptor(execution_id: str) -> DatasetDescriptor:
    return DatasetDescriptor(
        infrastructure=InfrastructureConfig(
            name=execution_id, descriptor_path="examples/tn_descriptor_elcm.yaml"
        ),
        auto_start_elcm=False,
    )


def _patch_common(monkeypatch, destroy_calls: list[str], state_result):
    async def _fake_generate(infra, execution_id):
        return "artifacts/desc.yaml"

    async def _fake_deploy(infra, execution_id=None, generated_descriptor_path=None):
        raise RuntimeError("simulated mis-detected TNLCM failure")

    async def _fake_get_tn_state(tn_id, client=None):
        return state_result

    async def _fake_destroy(tn_id, execution_id=None):
        destroy_calls.append(tn_id)

    def _fake_down_tunnel(tn_id, conf_path=None):
        return None

    monkeypatch.setattr(tnlcm_phase, "generate_tnlcm_descriptor", _fake_generate)
    monkeypatch.setattr("app.adapters.tnlcm.deploy_trial_network", _fake_deploy)
    monkeypatch.setattr("app.adapters.tnlcm.get_tn_state", _fake_get_tn_state)
    monkeypatch.setattr("app.adapters.tnlcm.destroy_trial_network", _fake_destroy)
    monkeypatch.setattr("app.adapters.wireguard.down_tunnel", _fake_down_tunnel)


@pytest.mark.asyncio
async def test_tnlcm_phase_failure_keeps_healthy_activated_tn(monkeypatch):
    """Fallo con la TN 'activated': se conserva (no destroy) y queda direccionable."""
    execution_id = "tn-reconcile-keep"
    destroy_calls: list[str] = []
    _patch_common(monkeypatch, destroy_calls, state_result="activated")
    _pending_record(execution_id)

    await tnlcm_phase.run_tnlcm_phase(execution_id, _descriptor(execution_id))

    record = state.executions[execution_id]
    assert record.status == ExecutionState.failed
    # tn_id registrado pronto: la TN sigue direccionable para re-POST / DELETE.
    assert record.tn_id == execution_id
    # La TN sana NO se destruye.
    assert destroy_calls == []


@pytest.mark.asyncio
async def test_tnlcm_phase_failure_destroys_terminal_tn(monkeypatch):
    """Fallo con la TN en estado terminal ('failed'): sí se destruye (comportamiento previo)."""
    execution_id = "tn-reconcile-destroy"
    destroy_calls: list[str] = []
    _patch_common(monkeypatch, destroy_calls, state_result="failed")
    _pending_record(execution_id)

    await tnlcm_phase.run_tnlcm_phase(execution_id, _descriptor(execution_id))

    record = state.executions[execution_id]
    assert record.status == ExecutionState.failed
    assert destroy_calls == [execution_id]
