"""Tests de los endpoints bloqueantes: señales de fase, tope de espera y dataset parcial.

Los tres endpoints de ciclo de vida (POST /executions, POST /elcm y DELETE /tn)
no responden hasta que su fase alcanza un estado terminal. El mecanismo es un
`asyncio.Event` por fase que la background task activa siempre, haya ido bien o
mal; al agotarse el tope de espera se responde 504 y la fase continua.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.api.phases import phase_http_status
from app.main import app
from app.domain.descriptor import ExperimentConfig
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord
from app.services import state
from app.services import background
from app.services.phases import results
from app.services import reporting
from app.services.phases import elcm as elcm_phase
from app.services.phases import teardown as teardown_phase

client = TestClient(app)


def _headers() -> dict[str, str]:
    return {"x-api-key": settings.api_key}


ELCM_BODY = {
    "experiment": {
        "name": "exp-block",
        "testcase_paths": ["TC_1_Preflight.yml"],
        "ues_paths": [],
    }
}


@pytest.fixture(autouse=True)
def isolated_executions(monkeypatch):
    """Aisla el estado en memoria y evita disco y background tasks reales."""
    monkeypatch.setattr(state, "executions", {})
    monkeypatch.setattr(state, "save_to_disk", lambda: None)
    monkeypatch.setattr(state, "_tnlcm_deploy_in_progress", None)
    yield


def _add_record(execution_id: str, status: ExecutionState, **kwargs) -> ExecutionRecord:
    record = ExecutionRecord(execution_id=execution_id, status=status, **kwargs)
    state.executions[execution_id] = record
    return record


# ---------------------------------------------------------------------------
# Espera y tope en los endpoints
# ---------------------------------------------------------------------------


def test_elcm_waits_until_the_phase_signals_and_returns_200(monkeypatch) -> None:
    """Con wait=true la respuesta llega cuando la fase activa su señal."""
    _add_record("tn-a", ExecutionState.tn_ready, tn_id="tn-a")

    def _fake_spawn(coro, *, name: str):
        coro.close()
        # Simula una fase que termina bien: TN lista y dataset completo.
        state.set_experiment_run_fields("tn-a", "exp-block", status="FINISHED")
        state.update("tn-a", status=ExecutionState.tn_ready, message="Experiment finished")
        state.signal_phase("tn-a", "_experiment_finished")
        return None

    monkeypatch.setattr(background, "spawn_background_task", _fake_spawn)

    response = client.post("/executions/tn-a/elcm", json=ELCM_BODY, headers=_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "TN_READY"
    assert body["message"] == "Experiment finished"


def test_elcm_returns_504_when_the_phase_outlives_the_cap(monkeypatch) -> None:
    """Agotado el tope se responde 504; la fase sigue viva en segundo plano."""
    _add_record("tn-a", ExecutionState.tn_ready, tn_id="tn-a")

    def _fake_spawn(coro, *, name: str):
        coro.close()  # La fase nunca activa la señal.
        return None

    monkeypatch.setattr(background, "spawn_background_task", _fake_spawn)
    monkeypatch.setattr("app.services.orchestrator.ELCM_PHASE_MAX_WAIT_SECONDS", 0.05)

    response = client.post("/executions/tn-a/elcm", json=ELCM_BODY, headers=_headers())

    assert response.status_code == 504
    detail = response.json()["detail"]
    assert "still running" in detail
    assert "GET /executions/tn-a" in detail
    # El estado no se pierde: la ejecucion sigue registrada y en curso.
    assert state.executions["tn-a"].status == ExecutionState.running_experiment


def test_wait_false_returns_202_without_blocking(monkeypatch) -> None:
    """El escape wait=false conserva el comportamiento no bloqueante."""
    _add_record("tn-a", ExecutionState.tn_ready, tn_id="tn-real-id")

    def _fake_spawn(coro, *, name: str):
        coro.close()
        return None

    monkeypatch.setattr(background, "spawn_background_task", _fake_spawn)

    response = client.delete("/executions/tn-a/tn?wait=false", headers=_headers())

    assert response.status_code == 202
    assert response.json()["status"] == "DESTROYING"


def test_second_experiment_rearms_the_signal(monkeypatch) -> None:
    """La señal del experimento anterior no puede desbloquear al siguiente."""
    record = _add_record("tn-a", ExecutionState.tn_ready, tn_id="tn-a")
    state.signal_phase("tn-a", "_experiment_finished")
    assert record._experiment_finished.is_set()

    def _fake_spawn(coro, *, name: str):
        coro.close()
        return None

    monkeypatch.setattr(background, "spawn_background_task", _fake_spawn)
    monkeypatch.setattr("app.services.orchestrator.ELCM_PHASE_MAX_WAIT_SECONDS", 0.05)

    response = client.post("/executions/tn-a/elcm", json=ELCM_BODY, headers=_headers())

    # Si no se hubiera rearmado, la señal vieja habria devuelto 200 al instante.
    assert response.status_code == 504


# ---------------------------------------------------------------------------
# El codigo HTTP es el desenlace
# ---------------------------------------------------------------------------


def _spawn_that_finishes_with(**run_fields):
    """Fake spawn que cierra el experimento como lo hace la fase real.

    run_elcm_phase deja el detalle en los dos sitios: en el ExperimentRun y en
    el record (de donde lo lee la respuesta).
    """

    def _fake_spawn(coro, *, name: str):
        coro.close()
        state.set_experiment_run_fields("tn-a", "exp-block", **run_fields)
        state.update("tn-a", status=ExecutionState.tn_ready, error=run_fields.get("error"))
        state.signal_phase("tn-a", "_experiment_finished")
        return None

    return _fake_spawn


def test_failed_experiment_responds_502(monkeypatch) -> None:
    """Un experimento fallido no puede devolver 200 con FAILED en el body."""
    _add_record("tn-a", ExecutionState.tn_ready, tn_id="tn-a")
    monkeypatch.setattr(
        background,
        "spawn_background_task",
        _spawn_that_finishes_with(status="FAILED", error="ELCM /experiment/run (HTTP 400)"),
    )

    response = client.post("/executions/tn-a/elcm", json=ELCM_BODY, headers=_headers())

    assert response.status_code == 502
    assert "HTTP 400" in response.json()["error"]


def test_partial_dataset_responds_207(monkeypatch) -> None:
    """Dataset a medias: ni 200 limpio ni error, el codigo lo distingue."""
    _add_record("tn-a", ExecutionState.tn_ready, tn_id="tn-a")
    monkeypatch.setattr(
        background,
        "spawn_background_task",
        _spawn_that_finishes_with(status="FINISHED", error="Partial dataset: missing raw"),
    )

    response = client.post("/executions/tn-a/elcm", json=ELCM_BODY, headers=_headers())

    assert response.status_code == 207
    assert "missing raw" in response.json()["error"]


@pytest.mark.parametrize(
    ("vpn_status", "state", "expected"),
    [
        ("UP", ExecutionState.tn_ready, 200),
        ("MANUAL_REQUIRED", ExecutionState.tn_ready, 207),
        (None, ExecutionState.failed, 502),
    ],
)
def test_vpn_phase_status_mapping(vpn_status, state, expected) -> None:
    record = ExecutionRecord(execution_id="tn-a", status=state, tn_id="tn-a", vpn_status=vpn_status)
    assert phase_http_status(record, "_vpn_ready") == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [(ExecutionState.destroyed, 200), (ExecutionState.failed, 502)],
)
def test_teardown_phase_status_mapping(state, expected) -> None:
    record = ExecutionRecord(execution_id="tn-a", status=state, tn_id="tn-a")
    assert phase_http_status(record, "_tn_purged") == expected


# ---------------------------------------------------------------------------
# Garantia de que la señal se activa siempre
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_signals_even_when_the_phase_raises(monkeypatch) -> None:
    """Una excepcion inesperada no puede dejar al DELETE esperando al tope."""
    record = _add_record("tn-a", ExecutionState.destroying, tn_id="tn-a")

    async def _boom(execution_id: str) -> None:
        raise RuntimeError("unexpected teardown crash")

    monkeypatch.setattr(teardown_phase, "_run_teardown_phase_inner", _boom)

    with pytest.raises(RuntimeError, match="unexpected teardown crash"):
        await teardown_phase.run_teardown_phase("tn-a")

    assert record._tn_purged.is_set()


# ---------------------------------------------------------------------------
# Fase ELCM: estados terminales y dataset acotado
# ---------------------------------------------------------------------------


def _mock_elcm_phase(monkeypatch, *, status: str, collect=None) -> None:
    """Mocks minimos para llevar run_elcm_phase hasta el bucle de polling."""

    async def _noop(*args, **kwargs):
        return None

    async def _run_experiment(*args, **kwargs):
        return "42"

    async def _get_status(*args, **kwargs):
        return status

    monkeypatch.setattr(
        "app.adapters.elcm.resolve_testcase_file",
        lambda ref: f"templates/ELCM/TestCase/{ref}",
    )
    monkeypatch.setattr("app.adapters.elcm.generate_experiment_descriptor", _noop)
    monkeypatch.setattr("app.adapters.elcm.upload_test_cases", _noop)
    monkeypatch.setattr("app.adapters.elcm.run_experiment", _run_experiment)
    monkeypatch.setattr("app.adapters.elcm.get_experiment_status", _get_status)
    monkeypatch.setattr(reporting, "persist_telemetry_report_best_effort", _noop)
    if collect is not None:
        monkeypatch.setattr("app.adapters.elcm.collect_results", collect)


def _elcm_ready_record(execution_id: str) -> ExecutionRecord:
    record = ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.running_experiment,
        tn_id=execution_id,
        elcm_base_url="http://192.168.199.3:5000",
        vpn_status="UP",
    )
    state.executions[execution_id] = record
    return record


@pytest.mark.asyncio
async def test_cancelled_status_is_terminal(monkeypatch) -> None:
    """'Cancelled' corta el bucle en vez de girar hasta el timeout de 1 h."""
    record = _elcm_ready_record("tn-cancel")
    _mock_elcm_phase(monkeypatch, status="Cancelled")
    monkeypatch.setattr(elcm_phase, "ELCM_POLL_INTERVAL_SECONDS", 0.01)

    await elcm_phase.run_elcm_phase(
        "tn-cancel", ExperimentConfig(name="exp-c", testcase_paths=["TC_1_Preflight.yml"])
    )

    assert record.status == ExecutionState.tn_ready
    assert "did not complete" in (record.error or "")
    assert "Cancelled" in (record.error or "")
    assert record._experiment_finished.is_set()


@pytest.mark.asyncio
async def test_unreadable_err_status_is_terminal(monkeypatch) -> None:
    """'ERR' (lapida ilegible en ELCM) tambien es terminal."""
    record = _elcm_ready_record("tn-err")
    _mock_elcm_phase(monkeypatch, status="ERR")
    monkeypatch.setattr(elcm_phase, "ELCM_POLL_INTERVAL_SECONDS", 0.01)

    await elcm_phase.run_elcm_phase(
        "tn-err", ExperimentConfig(name="exp-e", testcase_paths=["TC_1_Preflight.yml"])
    )

    assert record.status == ExecutionState.tn_ready
    assert "did not complete" in (record.error or "")


@pytest.mark.asyncio
async def test_dataset_timeout_leaves_a_partial_result(monkeypatch) -> None:
    """El tope del dataset no es un fallo: conserva lo recolectado y lo declara."""
    record = _elcm_ready_record("tn-partial")

    async def _slow_collect(*args, **kwargs):
        await asyncio.sleep(5)
        return {}

    _mock_elcm_phase(monkeypatch, status="Finished", collect=_slow_collect)
    monkeypatch.setattr(elcm_phase, "DATASET_MAX_SECONDS", 0.05)

    await elcm_phase.run_elcm_phase(
        "tn-partial", ExperimentConfig(name="exp-p", testcase_paths=["TC_1_Preflight.yml"])
    )

    # El experimento SI termino: la TN sigue viva y el error describe lo que falto.
    assert record.status == ExecutionState.tn_ready
    assert "Partial dataset" in (record.error or "")
    assert "missing logs" in (record.error or "")
    assert record._experiment_finished.is_set()


@pytest.mark.asyncio
async def test_dataset_artifacts_are_flushed_incrementally(monkeypatch) -> None:
    """Cada salida se vuelca al record en cuanto existe, no al final del bloque."""
    record = _elcm_ready_record("tn-flush")
    # Dos salidas para poder observar el record a mitad de la recoleccion.
    record.dataset_output = ["logs", "raw"]
    seen_during_raw: list[list[str]] = []

    async def _collect(*args, **kwargs):
        return {"logs": {}}

    async def _build_artifacts(*args, **kwargs):
        return ["artifacts/tn-flush/result/logs.json"]

    async def _capture_raw(*args, **kwargs):
        # Al llegar aqui, los artefactos de logs ya tienen que estar volcados.
        seen_during_raw.append(list(state.executions["tn-flush"].artifacts))
        return ["artifacts/tn-flush/result/raw.csv"]

    _mock_elcm_phase(monkeypatch, status="Finished", collect=_collect)
    monkeypatch.setattr(elcm_phase.artifacts, "build_artifacts", _build_artifacts)
    monkeypatch.setattr(results, "collect_raw", _capture_raw)

    await elcm_phase.run_elcm_phase(
        "tn-flush", ExperimentConfig(name="exp-f", testcase_paths=["TC_1_Preflight.yml"])
    )

    assert len(seen_during_raw) == 1
    assert any(
        "logs.json" in path for path in seen_during_raw[0]
    ), "los artefactos de logs deberian estar en el record antes de recolectar raw"
    assert any("raw.csv" in path for path in record.artifacts)
