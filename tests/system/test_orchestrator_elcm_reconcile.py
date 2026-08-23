"""Tests de la reconciliacion de /elcm con el estado real de la TN en TNLCM.

El record y TNLCM pueden divergir: un teardown que no llego a purgar, o un fallo
posterior al despliegue, dejan el record en DESTROYED/FAILED con la TN todavia
en pie. Antes eso bloqueaba /elcm con un 409 permanente y el unico camino para
corregir el body era re-POSTear /executions. La fuente de verdad es TNLCM.
"""

import httpx
import pytest

from app.services import state
from app.services import orchestrator
from app.domain.descriptor import DatasetRequest, ExperimentConfig
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord
from app.services import errors
from app.services.phases import tnlcm as tnlcm_phase

pytestmark = pytest.mark.usefixtures("isolate_orchestrator_state")


@pytest.fixture
def tunnel_calls(monkeypatch, tmp_path):
    """Sustituye WireGuard y deja el .conf en disco para que se pueda reabrir."""
    calls: list[tuple[str, str]] = []
    conf_path = tmp_path / "tn-a.conf"
    conf_path.write_text("[Interface]\n", encoding="utf-8")

    def _fake_up(tn_id: str, path: str) -> None:
        calls.append((tn_id, path))

    monkeypatch.setattr("app.adapters.wireguard.up_tunnel", _fake_up)
    return calls, str(conf_path)


def _record(status: ExecutionState, **kwargs) -> ExecutionRecord:
    record = ExecutionRecord(execution_id="tn-a", status=status, tn_id="tn-a", **kwargs)
    state.executions["tn-a"] = record
    return record


def _patch_tn_state(monkeypatch, state):
    async def _fake_get_tn_state(tn_id, client=None):
        if isinstance(state, Exception):
            raise state
        return state

    monkeypatch.setattr("app.adapters.tnlcm.get_tn_state", _fake_get_tn_state)


async def _start(name: str = "exp-fix"):
    return await orchestrator.start_elcm_phase(
        "tn-a",
        ExperimentConfig(name=name, testcase_paths=["TC_1_Preflight.yml"]),
        DatasetRequest(output=["logs"]),
    )


@pytest.mark.parametrize(
    ("status", "tn_state"),
    [
        (ExecutionState.destroyed, "activated"),
        (ExecutionState.destroyed, "created"),
        (ExecutionState.failed, "activated"),
    ],
)
@pytest.mark.asyncio
async def test_elcm_recovers_when_tnlcm_still_has_the_tn(
    monkeypatch, tunnel_calls, status, tn_state
) -> None:
    calls, conf_path = tunnel_calls
    _record(status, vpn_conf_path=conf_path, vpn_status="DOWN", error="TestCase file not found")
    _patch_tn_state(monkeypatch, tn_state)

    record = await _start()

    assert record.status is ExecutionState.running_experiment
    assert [run.name for run in record.experiments] == ["exp-fix"]
    # El tunel se reabre: sin el, la TN sigue viva pero es inalcanzable.
    assert calls == [("tn-a", conf_path)]
    assert record.vpn_status == "UP"
    # El error de la ejecucion anterior no se arrastra al experimento nuevo.
    assert record.error is None


@pytest.mark.asyncio
async def test_elcm_still_rejected_when_tn_is_really_gone(monkeypatch, tunnel_calls) -> None:
    calls, conf_path = tunnel_calls
    _record(ExecutionState.destroyed, vpn_conf_path=conf_path)
    _patch_tn_state(monkeypatch, None)  # 404 en TNLCM

    with pytest.raises(errors.ExecutionConflictError, match="POST /executions"):
        await _start()

    assert calls == []
    assert state.executions["tn-a"].status is ExecutionState.destroyed


@pytest.mark.asyncio
async def test_elcm_rejected_when_tnlcm_state_is_terminal(monkeypatch) -> None:
    _record(ExecutionState.failed)
    _patch_tn_state(monkeypatch, "purged")

    with pytest.raises(errors.ExecutionConflictError):
        await _start()


@pytest.mark.asyncio
async def test_unreachable_tnlcm_does_not_recover_the_record(monkeypatch) -> None:
    """Sin respuesta de TNLCM no se puede afirmar que la TN siga viva."""
    _record(ExecutionState.destroyed)
    _patch_tn_state(monkeypatch, httpx.ConnectError("tnlcm down"))

    with pytest.raises(errors.ExecutionConflictError):
        await _start()

    assert state.executions["tn-a"].status is ExecutionState.destroyed


@pytest.mark.asyncio
async def test_experiment_is_accepted_even_if_the_tunnel_cannot_be_reopened(
    monkeypatch, tunnel_calls
) -> None:
    """Igual que en el despliegue: MANUAL_REQUIRED no impide lanzar experimentos."""
    _, conf_path = tunnel_calls
    _record(ExecutionState.destroyed, vpn_conf_path=conf_path)
    _patch_tn_state(monkeypatch, "activated")

    def _fail_up(tn_id: str, path: str) -> None:
        raise tnlcm_phase.wireguard.WireGuardError("access is denied")

    monkeypatch.setattr("app.adapters.wireguard.up_tunnel", _fail_up)

    record = await _start()

    assert record.status is ExecutionState.running_experiment
    assert record.vpn_status == "MANUAL_REQUIRED"
    assert "access is denied" in (record.vpn_error or "")


@pytest.mark.asyncio
async def test_missing_conf_file_marks_manual_required(monkeypatch, tmp_path) -> None:
    _record(ExecutionState.destroyed, vpn_conf_path=str(tmp_path / "no_existe.conf"))
    _patch_tn_state(monkeypatch, "activated")

    record = await _start()

    assert record.status is ExecutionState.running_experiment
    assert record.vpn_status == "MANUAL_REQUIRED"


@pytest.mark.asyncio
async def test_states_outside_the_reconcilable_set_are_not_probed(monkeypatch) -> None:
    """DEPLOYING o un experimento en curso no se reconcilian: no hay nada que recuperar."""
    _record(ExecutionState.deploying)

    async def _should_not_run(tn_id, client=None):
        raise AssertionError("TNLCM must not be probed from a non-reconcilable state")

    monkeypatch.setattr("app.adapters.tnlcm.get_tn_state", _should_not_run)

    with pytest.raises(errors.ExecutionConflictError, match="not ready"):
        await _start()


@pytest.mark.asyncio
async def test_ready_tn_is_not_probed(monkeypatch) -> None:
    _record(ExecutionState.tn_ready)

    async def _should_not_run(tn_id, client=None):
        raise AssertionError("a ready TN needs no reconciliation")

    monkeypatch.setattr("app.adapters.tnlcm.get_tn_state", _should_not_run)

    record = await _start()

    assert record.status is ExecutionState.running_experiment


@pytest.mark.asyncio
async def test_missing_execution_raises_not_found() -> None:
    with pytest.raises(errors.ExecutionNotFoundError):
        await _start()
