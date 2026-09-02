"""Fase de conexion: aparta una TN sin borrarla y vuelve a ella cuando toca.

Pausar es una operacion **local**: en TNLCM la TN sigue viva y 'activated', y lo
unico que se baja es el tunel WireGuard, para que otra TN pueda usar el suyo sin
que las rutas se pisen. No se destruye nada, asi que volver cuesta segundos en
vez de un redespliegue entero.

Por eso ninguna de las dos operaciones lleva descriptor: reconectar no vuelve a
generar nada, se limita a levantar el tunel del `.conf` que ya guardo la fase
TNLCM. Ese es el motivo de que vivan aqui y no en `POST /executions`, que
recrearia el `ExecutionRecord` y reescribiria el `dataset_descriptor.yaml`
original.

Son sincronas: no hay tarea en segundo plano ni señal de fase que esperar,
duran lo que tarde WireGuard (y, en Windows, el dialogo UAC).
"""

import asyncio
import logging

import httpx

from app.adapters import tnlcm, wireguard
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord
from app.observability.telemetry import telemetry
from app.services import state
from app.services.errors import ExecutionConflictError, ExecutionNotFoundError
from app.services.phases import tnlcm as tnlcm_phase

logger = logging.getLogger(__name__)

# Estados desde los que se admite reconectar. TN_READY entra para que la
# operacion sea idempotente, y FAILED/DESTROYED (RECONCILABLE_STATES) para que
# `resume` sea la forma explicita de recuperar una TN que TNLCM sigue teniendo
# viva: hasta ahora eso solo pasaba de tapadillo al llamar a /elcm.
RESUMABLE_STATES = frozenset({ExecutionState.paused, ExecutionState.tn_ready}) | frozenset(
    state.RECONCILABLE_STATES
)


def _require_tn(execution_id: str) -> ExecutionRecord:
    record = state.executions.get(execution_id)
    if not record:
        raise ExecutionNotFoundError(f"Execution '{execution_id}' not found")
    if not record.tn_id:
        raise ExecutionNotFoundError(
            f"Execution '{execution_id}' has no TN to connect to (tn_id missing)"
        )
    return record


async def pause_tn(execution_id: str) -> ExecutionRecord:
    """Baja el tunel de una TN y la deja apartada, sin tocar TNLCM.

    Si el tunel no se puede bajar la ejecucion queda PAUSED igualmente, con
    `vpn_status=DOWN_ERROR`: dejarla en TN_READY la haria pasar por conectada
    cuando ya nadie cuenta con ella. El endpoint responde 207 para que el aviso
    no pase desapercibido y el tunel se pueda comprobar a mano.
    """
    record = _require_tn(execution_id)

    if record.status in {ExecutionState.running_experiment, ExecutionState.collecting}:
        raise ExecutionConflictError(
            "An experiment is currently running on this TN; wait for it to finish"
        )
    if record.status is ExecutionState.paused:
        raise ExecutionConflictError(f"Execution '{execution_id}' is already paused")
    if record.status is not ExecutionState.tn_ready:
        raise ExecutionConflictError(
            f"TN cannot be paused in its current state (status: {record.status.value})"
        )

    tn_id = record.tn_id
    interface = record.vpn_interface or tn_id
    pause_timer = telemetry.start_timer("wireguard", "tunnel_down", execution_id)
    pause_timer.start()
    try:
        # A un hilo: `down_tunnel` lanza subprocesos bloqueantes (§8.1).
        await asyncio.to_thread(wireguard.down_tunnel, interface, record.vpn_conf_path)
    except wireguard.WireGuardError as exc:
        pause_timer.stop(status="error")
        logger.warning(
            "[%s] Could not bring WireGuard tunnel %s down: %s", execution_id, interface, exc
        )
        state.update(
            execution_id,
            status=ExecutionState.paused,
            vpn_status="DOWN_ERROR",
            vpn_error=str(exc),
            message=(
                f"TN {tn_id} paused, but the WireGuard tunnel could not be brought down; "
                "check it manually before connecting another TN"
            ),
        )
        return state.executions[execution_id]

    pause_timer.stop(status="success")
    state.update(
        execution_id,
        status=ExecutionState.paused,
        vpn_status="DOWN",
        vpn_error=None,
        message=f"TN {tn_id} paused: tunnel down, TN still alive in TNLCM",
    )
    telemetry.increment_counter(
        "tn_connection_total", labels={"service": "orchestrator", "operation": "pause"}
    )
    logger.info("[%s] TN %s paused (tunnel %s down)", execution_id, tn_id, interface)
    return state.executions[execution_id]


async def resume_tn(execution_id: str) -> ExecutionRecord:
    """Vuelve a conectar con una TN pausada y la deja lista para experimentos.

    No regenera nada: reabre el tunel con el `.conf` que ya existe y devuelve el
    record a TN_READY, con lo que el descriptor original y el historial de
    experimentos se conservan intactos.
    """
    record = _require_tn(execution_id)

    if record.status is ExecutionState.tn_ready and record.vpn_status == "UP":
        return record
    if record.status not in RESUMABLE_STATES:
        raise ExecutionConflictError(
            f"TN cannot be resumed in its current state (status: {record.status.value})"
        )

    # Un solo tunel a la vez: con dos arriba las rutas pueden pisarse y el
    # experimento acabaria hablando con la TN equivocada.
    connected = state.connected_execution(exclude=execution_id)
    if connected is not None:
        raise ExecutionConflictError(
            f"Execution '{connected}' still has its WireGuard tunnel up; pause it first "
            f"with POST /executions/{connected}/pause"
        )

    tn_id = record.tn_id
    tnlcm_unreachable = False
    try:
        tn_state = await tnlcm.get_tn_state(tn_id)
    except (httpx.HTTPError, ValueError) as exc:
        # Reabrir el tunel no necesita a TNLCM y la reconexion se ha pedido de
        # forma explicita: se sigue adelante avisando, en vez de negarla porque
        # el inventario no conteste.
        logger.warning(
            "[%s] Could not read TN %s state before resume: %s", execution_id, tn_id, exc
        )
        tn_state = None
        tnlcm_unreachable = True

    if not tnlcm_unreachable and tn_state not in (
        tnlcm.TN_STATE_CREATED | tnlcm.TN_STATE_ACTIVATED
    ):
        detail = f"'{tn_state}'" if tn_state else "gone"
        raise ExecutionConflictError(
            f"TN {tn_id} is {detail} in TNLCM; there is nothing to reconnect to. "
            "Deploy it again with POST /executions"
        )

    await tnlcm_phase.ensure_tunnel_up(execution_id)

    if tnlcm_unreachable:
        message = f"TN {tn_id} reconnected (TNLCM did not answer, its state could not be confirmed)"
    else:
        message = f"TN {tn_id} reconnected ('{tn_state}' in TNLCM); ready for experiments"

    state.update(execution_id, status=ExecutionState.tn_ready, error=None, message=message)
    telemetry.increment_counter(
        "tn_connection_total", labels={"service": "orchestrator", "operation": "resume"}
    )
    logger.info("[%s] TN %s resumed", execution_id, tn_id)
    return state.executions[execution_id]
