"""Fase de borrado: baja el tunel y destruye + purga la TN en TNLCM.

Es la unica fase que se ejecuta tambien de forma automatica: una TN efimera
(`ephemeral_tn`) la dispara al terminar su primer experimento.
"""

import asyncio
import logging

from app.adapters import tnlcm, wireguard
from app.domain.enums import ExecutionState
from app.observability.telemetry import telemetry
from app.services import reporting, state

logger = logging.getLogger(__name__)


async def run_teardown_phase(execution_id: str) -> None:
    """Bloque de borrado de la TN: baja el tunel WireGuard y ejecuta deleted + purged.

    Unica pieza del sistema que destruye una TN operativa; la invocan el
    pipeline efimero (ephemeral_tn=true) y el endpoint DELETE de borrado manual.

    Envuelve al cuerpo real para garantizar que la señal se activa por
    cualquier salida, incluida una excepcion inesperada: si no, el DELETE
    bloqueante se quedaria esperando hasta agotar su tope.
    """
    try:
        await _run_teardown_phase_inner(execution_id)
    finally:
        state.signal_phase(execution_id, "_tn_purged")


async def _run_teardown_phase_inner(execution_id: str) -> None:
    record = state.executions.get(execution_id)
    if not record or not record.tn_id:
        logger.warning("[%s] Teardown requested but there is no TN to destroy", execution_id)
        return

    tn_id = record.tn_id
    state.update(
        execution_id,
        status=ExecutionState.destroying,
        message=f"Destroying TN {tn_id} (deleted + purged)",
    )

    vpn_interface = record.vpn_interface or tn_id
    # Una TN pausada ya tiene el tunel abajo: volver a bajarlo falla y dejaria un
    # DOWN_ERROR puramente cosmetico en el resumen del borrado.
    already_down = record.vpn_status == "DOWN"
    if vpn_interface and not already_down:
        vpn_down_timer = telemetry.start_timer("wireguard", "tunnel_down", execution_id)
        vpn_down_timer.start()
        try:
            logger.info(f"[{execution_id}] Teardown: deactivating WireGuard tunnel {vpn_interface}")
            await asyncio.to_thread(wireguard.down_tunnel, vpn_interface, record.vpn_conf_path)
            state.update(execution_id, vpn_status="DOWN", vpn_error=None)
            vpn_down_timer.stop(status="success")
        except Exception as vpn_error:
            vpn_down_timer.stop(status="error")
            logger.error(f"[{execution_id}] WireGuard deactivation failed: {vpn_error}")
            state.update(execution_id, vpn_status="DOWN_ERROR", vpn_error=str(vpn_error))

    try:
        logger.info(f"[{execution_id}] Teardown: destroying TN {tn_id}")
        await tnlcm.destroy_trial_network(tn_id, execution_id=execution_id)
    except Exception as cleanup_error:
        # El timer global queda abierto: el borrado puede reintentarse con
        # otra llamada al endpoint DELETE (se admite desde estado FAILED).
        logger.error(f"[{execution_id}] TN teardown failed: {cleanup_error}")
        state.update(
            execution_id,
            status=ExecutionState.failed,
            error=str(cleanup_error),
            message=f"TN {tn_id} teardown failed: {cleanup_error}",
        )
        await reporting.persist_telemetry_report_best_effort(execution_id, "teardown_failed")
        return

    state.update(
        execution_id,
        status=ExecutionState.destroyed,
        message=f"TN {tn_id} destroyed and purged",
    )
    telemetry.increment_counter(
        "tn_teardown_total", labels={"service": "orchestrator", "status": "success"}
    )
    await reporting.persist_telemetry_report_best_effort(execution_id, "tn_destroyed")
    logger.info("[%s] TN %s teardown completed", execution_id, tn_id)

    # Cerrar timer global de la ejecucion: el ciclo de vida termina aqui
    final_record = state.executions.get(execution_id)
    execution_timer = getattr(final_record, "_execution_timer", None)
    if execution_timer is not None:
        execution_timer.stop(status="success")
        telemetry.log_event(
            "info",
            "orchestrator_execution.completed",
            service="orchestrator",
            operation="create",
            execution_id=execution_id,
            status="success",
        )
        telemetry.increment_counter(
            "orchestrator_execution_total",
            labels={"service": "orchestrator", "status": "success"},
        )
