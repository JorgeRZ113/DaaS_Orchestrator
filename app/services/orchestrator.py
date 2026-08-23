import logging
import time


from app.storage import artifacts
from app.domain.descriptor import (
    DatasetDescriptor,
    DatasetRequest,
    DescriptorSource,
    ExperimentConfig,
)
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord
from app.observability.telemetry import format_duration_display, telemetry

from app.services import background, state
from app.services.errors import (
    ExecutionConflictError,
    ExecutionNotFoundError,
)
from app.services.phases import connection as connection_phase
from app.services.phases import elcm as elcm_phase
from app.services.phases import teardown as teardown_phase
from app.services.phases import tnlcm as tnlcm_phase

logger = logging.getLogger(__name__)


def get_execution(execution_id: str):
    """Delegacion fina hacia `state`, para que la API dependa solo de este modulo."""
    return state.get_execution(execution_id)


def list_executions():
    """Delegacion fina hacia `state` (ver `get_execution`)."""
    return state.list_executions()


async def pause_tn(execution_id: str) -> ExecutionRecord:
    """Delegacion fina hacia la fase de conexion (ver `get_execution`)."""
    return await connection_phase.pause_tn(execution_id)


async def resume_tn(execution_id: str) -> ExecutionRecord:
    """Delegacion fina hacia la fase de conexion (ver `get_execution`)."""
    return await connection_phase.resume_tn(execution_id)


async def wait_for_phase(execution_id: str, signal: str, timeout: float):
    """Delegacion fina hacia `state` (ver `get_execution`)."""
    return await state.wait_for_phase(execution_id, signal, timeout)


async def probe_tn_state(record: ExecutionRecord) -> str | None:
    """Delegacion fina hacia la fase TNLCM (ver `get_execution`).

    El estado que TNLCM reporta para la TN, best-effort: None si no hay TN o si
    no se pudo consultar.
    """
    return await tnlcm_phase.probe_tn_state(record)


# Topes de espera de los endpoints bloqueantes. Viven aqui, en el coordinador,
# porque son contrato con la capa HTTP: cadena de timeouts anidados
# cliente > MAX_WAIT del endpoint > tope de la fase. Al vencer se responde 504 y
# la fase sigue su curso en segundo plano.
TNLCM_PHASE_MAX_WAIT_SECONDS = 2400  # 40 min (TNLCM activate llega a 35)
ELCM_PHASE_MAX_WAIT_SECONDS = 4200  # 70 min (3600 de experimento + dataset)
TEARDOWN_MAX_WAIT_SECONDS = 3000  # 50 min

# El estado persistido se recupera al importar: un reinicio del proceso no debe
# perder de vista las ejecuciones anteriores.
state.load_from_disk()


async def create_tnlcm_execution(
    descriptor: DatasetDescriptor, source: DescriptorSource | None = None
) -> ExecutionRecord:
    """`source` indica en que formato llego el descriptor, para persistirlo igual.

    Es opcional para que cualquier llamante programatico (tests incluidos) pueda
    seguir invocando esto con el descriptor a secas.
    """
    execution_id = descriptor.infrastructure.name.strip()
    telemetry.increment_counter(
        "requests_total", labels={"service": "orchestrator", "operation": "create"}
    )

    # Timer end-to-end para toda la ejecución
    execution_timer = telemetry.start_timer(
        "orchestrator", "execution_total", execution_id=execution_id
    )
    execution_timer.start()
    telemetry.log_event(
        "info",
        "orchestrator_execution.started",
        service="orchestrator",
        operation="create",
        execution_id=execution_id,
    )

    # Measure lock wait time
    lock_start = time.time()
    state.acquire_tnlcm_deploy_slot(execution_id)
    lock_wait = time.time() - lock_start
    try:
        telemetry.observe_duration(
            service="orchestrator",
            operation="lock_wait",
            execution_id=execution_id,
            duration_seconds=lock_wait,
        )
        payload = {"service": "orchestrator", "operation": "lock", "execution_id": execution_id}
        if lock_wait >= 1.0:
            payload["duration_display"] = format_duration_display(lock_wait)
        telemetry.log_event("info", "tnlcm_lock.acquire.completed", **payload)
    except Exception:
        pass

    try:
        descriptor_paths = artifacts.persist_dataset_descriptor(execution_id, descriptor, source)

        record = ExecutionRecord(
            execution_id=execution_id,
            status=ExecutionState.pending,
            message="Execution created",
            # ephemeral_tn solo aplica con auto-start; en manual se ignora
            ephemeral_tn=descriptor.auto_start_elcm and descriptor.ephemeral_tn,
            dataset_output=list(descriptor.dataset.output),
            dataset_variables=dict(descriptor.dataset.variables()),
            artifacts=list(descriptor_paths),
        )
        state.executions[execution_id] = record
        logger.debug(
            "[%s] STATUS NONE -> %s | %s", execution_id, record.status.value, record.message
        )
        state.save_to_disk()  # Guarda al crear

        # Update telemetry: active executions gauge
        try:
            telemetry.change_gauge("active_executions", 1.0, labels={"service": "orchestrator"})
        except Exception:
            pass

        # Store execution timer for later closure at end of orchestration
        setattr(state.executions[execution_id], "_execution_timer", execution_timer)

        background.spawn_background_task(
            tnlcm_phase.run_tnlcm_phase(execution_id, descriptor), name=f"tnlcm:{execution_id}"
        )
        return record
    except Exception:
        execution_timer.stop(status="error")
        telemetry.log_event(
            "error",
            "orchestrator_execution.failed",
            service="orchestrator",
            operation="create",
            execution_id=execution_id,
        )
        telemetry.increment_counter(
            "errors_total", labels={"service": "orchestrator", "operation": "create"}
        )
        state.release_tnlcm_deploy_slot(execution_id)
        raise


async def start_elcm_phase(
    execution_id: str,
    experiment: ExperimentConfig,
    dataset: DatasetRequest,
    source: DescriptorSource | None = None,
) -> ExecutionRecord:
    """Lanza un experimento manual sobre la TN viva (endpoint /elcm).

    Antes de decidir, reconcilia el record con TNLCM: si la TN sigue viva, un
    record en DESTROYED/FAILED se recupera a TN_READY para poder corregir el
    body y relanzar el experimento sin volver a desplegar por /executions. La
    transicion a RUNNING_EXPERIMENT sigue siendo sincrona (`_begin_experiment`),
    asi que dos peticiones concurrentes no pueden solapar experimentos.

    ephemeral_tn no aplica en el camino manual: la TN queda viva al terminar.
    `dataset` es la salida de datos pedida para este experimento concreto.
    """
    if execution_id not in state.executions:
        raise ExecutionNotFoundError(f"Execution '{execution_id}' not found")

    await tnlcm_phase._reconcile_live_tn(execution_id)
    record = elcm_phase._begin_experiment(execution_id, experiment, dataset, ephemeral=False)

    # Despues de `_begin_experiment`: si el nombre esta repetido o hay otro
    # experimento en curso, la peticion se rechaza y no debe dejar rastro.
    artifacts.persist_experiment_request(execution_id, experiment, dataset, source)
    return record


def start_tn_teardown(execution_id: str) -> ExecutionRecord:
    """Valida y lanza el borrado manual de la TN (endpoint DELETE).

    La transicion a DESTROYING se hace aqui de forma sincrona para que dos
    peticiones de borrado concurrentes no dupliquen los jobs de destroy/purge.
    """
    record = state.executions.get(execution_id)
    if not record:
        raise ExecutionNotFoundError(f"Execution '{execution_id}' not found")
    if not record.tn_id:
        raise ExecutionNotFoundError(
            f"Execution '{execution_id}' has no TN to remove (tn_id missing)"
        )
    if record.status in {ExecutionState.running_experiment, ExecutionState.collecting}:
        raise ExecutionConflictError(
            "An experiment is currently running on this TN; wait for it to finish"
        )
    if record.status in {ExecutionState.destroying, ExecutionState.destroyed}:
        raise ExecutionConflictError(f"TN removal already {record.status.value}")
    # PAUSED entra: una TN apartada sigue viva en TNLCM y consumiendo recursos
    # del facility, asi que tiene que poder borrarse sin reconectarla antes.
    if record.status not in {
        ExecutionState.tn_ready,
        ExecutionState.paused,
        ExecutionState.failed,
    }:
        raise ExecutionConflictError(
            f"TN cannot be removed in its current state (status: {record.status.value})"
        )

    state.update(
        execution_id,
        status=ExecutionState.destroying,
        message=f"TN removal triggered for {record.tn_id}",
    )
    # Rearmar: el borrado se admite tambien desde FAILED, asi que puede
    # reintentarse con la señal ya activada por el intento anterior.
    state.clear_phase_signal(execution_id, "_tn_purged")
    background.spawn_background_task(
        teardown_phase.run_teardown_phase(execution_id), name=f"teardown:{execution_id}"
    )
    return state.executions[execution_id]
