"""Fase TNLCM: genera el descriptor, despliega la TN y levanta el tunel.

Al terminar, si el descriptor pidio `auto_start_elcm`, encadena directamente con
la fase ELCM: es el unico punto donde una fase arranca a la siguiente.
"""

import asyncio
import logging
from pathlib import Path

import httpx

from app.adapters import tnlcm, wireguard
from app.adapters.wireguard import WireGuardManualDeploymentRequired
from app.domain.descriptor import DatasetDescriptor
from app.domain.enums import ExecutionState
from app.observability.telemetry import telemetry
from app.rendering.tnlcm.renderer import generate_tnlcm_descriptor
from app.services import reporting, state
from app.services.phases.elcm import IMPLEMENTED_DATASET_OUTPUTS, _begin_experiment
from app.storage import artifacts

logger = logging.getLogger(__name__)


async def run_tnlcm_phase(execution_id: str, descriptor: DatasetDescriptor) -> None:
    """Phase 1: validate + deploy TN, then activate the WireGuard tunnel automatically."""
    tn_id: str | None = None
    vpn_conf_path: str | None = None

    # Timer para TNLCM total
    tnlcm_phase_timer = telemetry.start_timer(
        "orchestrator", "tnlcm_phase", execution_id=execution_id
    )
    tnlcm_phase_timer.start()

    try:
        state.update(
            execution_id, status=ExecutionState.validating, message="Validating descriptor"
        )
        await asyncio.sleep(1)

        unsupported = [
            fmt for fmt in descriptor.dataset.output if fmt not in IMPLEMENTED_DATASET_OUTPUTS
        ]
        if unsupported:
            raise ValueError(
                f"dataset.output not yet implemented: {', '.join(unsupported)}. "
                f"Currently supported: {', '.join(sorted(IMPLEMENTED_DATASET_OUTPUTS))}"
            )

        state.update(
            execution_id, status=ExecutionState.validating, message="Generating TNLCM descriptor"
        )
        tnlcm_descriptor_path = await generate_tnlcm_descriptor(
            descriptor.infrastructure, execution_id
        )

        state.update(
            execution_id, status=ExecutionState.deploying, message="Deploying Trial Network"
        )
        logger.info(f"[{execution_id}] Deploying TN: {descriptor.infrastructure.name}")

        # Registrar el tn_id en cuanto se conoce (antes de desplegar): si el deploy
        # falla a mitad, el record conserva la TN direccionable para poder
        # reconciliarla con un re-POST o borrarla con el endpoint DELETE.
        tn_id = tnlcm.resolve_tn_id(descriptor.infrastructure)
        state.update(execution_id, tn_id=tn_id)

        # Timer para TNLCM create
        tnlcm_create_timer = telemetry.start_timer(
            "orchestrator", "tnlcm_create", execution_id=execution_id
        )
        tnlcm_create_timer.start()
        telemetry.log_event(
            "info",
            "tnlcm.create.started",
            service="orchestrator",
            operation="tnlcm_create",
            execution_id=execution_id,
        )

        try:
            tn_id = await tnlcm.deploy_trial_network(
                descriptor.infrastructure,
                execution_id=execution_id,
                generated_descriptor_path=tnlcm_descriptor_path,
            )
        except TypeError as exc:
            if "generated_descriptor_path" not in str(exc):
                raise
            tn_id = await tnlcm.deploy_trial_network(
                descriptor.infrastructure,
                execution_id=execution_id,
            )

        tnlcm_create_timer.stop(status="success")
        telemetry.log_event(
            "info",
            "tnlcm.create.completed",
            service="orchestrator",
            operation="tnlcm_create",
            execution_id=execution_id,
        )
        telemetry.increment_counter("tnlcm_create_total", labels={"service": "orchestrator"})

        state.update(
            execution_id, status=ExecutionState.collecting, message="Downloading TNLCM report"
        )
        report_markdown = tnlcm.download_trial_network_report(tn_id, execution_id=execution_id)
        raw_report_path = await artifacts.build_tnlcm_raw_report_artifact(
            execution_id, report_markdown
        )
        report_markdown_from_file = Path(raw_report_path).read_text(encoding="utf-8")
        report_summary = tnlcm.summarize_trial_network_report(report_markdown_from_file)
        summary_report_path = await artifacts.build_tnlcm_summary_artifact(
            execution_id, tn_id, report_summary
        )
        report_artifacts = [tnlcm_descriptor_path, raw_report_path, summary_report_path]

        tn_init_summary = report_summary
        wireguard_config = None
        if isinstance(tn_init_summary, dict):
            wireguard_config = tn_init_summary.get("wireguard_client_config")
        if not isinstance(wireguard_config, str) or not wireguard_config.strip():
            raise ValueError("TNLCM report does not include wireguard_client_config")
        vpn_conf_path = wireguard.write_tunnel_conf(execution_id, tn_id, wireguard_config)
        state.update(
            execution_id,
            vpn_interface=tn_id,
            vpn_conf_path=vpn_conf_path,
            vpn_status="CONFIG_WRITTEN",
            vpn_error=None,
        )

        # Extract ELCM URL from report if available
        elcm_url = tnlcm.extract_elcm_url_from_report(report_summary)
        if not elcm_url:
            raise ValueError("TNLCM report does not include a valid ELCM backend URL")
        state.update(execution_id, elcm_base_url=elcm_url)
        logger.info(f"[{execution_id}] ELCM URL extracted from report: {elcm_url}")

        record_for_artifacts = state.executions[execution_id]
        merged_report_artifacts = list(
            dict.fromkeys([*record_for_artifacts.artifacts, *report_artifacts])
        )

        vpn_timer = telemetry.start_timer("wireguard", "tunnel_up", execution_id)
        vpn_timer.start()
        try:
            # A un hilo: `up_tunnel` lanza subprocesos y, en Windows, puede
            # esperar a un dialogo UAC. No puede bloquear el event loop (§8.1).
            await asyncio.to_thread(wireguard.up_tunnel, tn_id, vpn_conf_path)
        except WireGuardManualDeploymentRequired as vpn_error:
            vpn_timer.stop(status="error")
            manual_message = (
                "TN deployment completed, but WireGuard VPN could not be deployed automatically; "
                "deploy it manually before starting ELCM"
            )
            logger.warning(f"[{execution_id}] {manual_message}: {vpn_error}")
            state.update(
                execution_id,
                status=ExecutionState.tn_ready,
                tn_id=tn_id,
                artifacts=merged_report_artifacts,
                vpn_interface=tn_id,
                vpn_conf_path=vpn_conf_path,
                vpn_status="MANUAL_REQUIRED",
                vpn_error=str(vpn_error),
                message=manual_message,
            )
            tnlcm_phase_timer.stop(status="success")
            telemetry.log_event(
                "info",
                "tnlcm.phase.completed",
                service="orchestrator",
                operation="tnlcm_phase",
                execution_id=execution_id,
                tn_id=tn_id,
                status="manual_required",
            )
            await reporting.persist_telemetry_report_best_effort(
                execution_id, "tnlcm_manual_required"
            )
            return

        vpn_timer.stop(status="success")

        # Wait 1 second for WireGuard VPN to be fully activated before calling other components
        await asyncio.sleep(1)

        state.update(execution_id, vpn_status="UP", message="TN ready and WireGuard tunnel active")

        state.update(
            execution_id,
            status=ExecutionState.tn_ready,
            tn_id=tn_id,
            artifacts=merged_report_artifacts,
            message="TN deployment completed with automatic WireGuard tunnel",
        )
        tnlcm_phase_timer.stop(status="success")
        telemetry.log_event(
            "info",
            "tnlcm.phase.completed",
            service="orchestrator",
            operation="tnlcm_phase",
            execution_id=execution_id,
            tn_id=tn_id,
            status="success",
        )
        telemetry.increment_counter(
            "tnlcm_phase_total", labels={"service": "orchestrator", "status": "success"}
        )
        await reporting.persist_telemetry_report_best_effort(execution_id, "tnlcm_completed")

        # Auto-start del primer experimento si esta configurado. ephemeral_tn
        # solo aplica en este camino: con auto_start_elcm=False se ignora y la
        # TN queda viva en TN_READY hasta que llegue /elcm o el borrado manual.
        if descriptor.auto_start_elcm and descriptor.experiment is not None:
            logger.info(f"[{execution_id}] Auto-starting ELCM phase")
            _begin_experiment(
                execution_id,
                descriptor.experiment,
                descriptor.dataset,
                ephemeral=descriptor.ephemeral_tn,
            )

        logger.info(
            f"[{execution_id}] TN {tn_id} deployment completed with active WireGuard tunnel."
        )

    except Exception as exc:
        logger.error(f"[{execution_id}] TNLCM phase error: {exc}")
        tnlcm_phase_timer.stop(status="error")
        telemetry.log_event(
            "error",
            "tnlcm.phase.failed",
            service="orchestrator",
            operation="tnlcm_phase",
            execution_id=execution_id,
            error=str(exc),
        )
        telemetry.increment_counter(
            "errors_total", labels={"service": "orchestrator", "operation": "tnlcm_phase"}
        )
        state.update(
            execution_id, status=ExecutionState.failed, error=str(exc), message=f"Error: {exc}"
        )
        await reporting.persist_telemetry_report_best_effort(execution_id, "tnlcm_failed")
        if tn_id:
            try:
                if vpn_conf_path:
                    await asyncio.to_thread(wireguard.down_tunnel, tn_id, vpn_conf_path)
            except Exception as cleanup_error:
                logger.warning(
                    f"[{execution_id}] WireGuard cleanup after TNLCM failure failed: {cleanup_error}"
                )

            # Cleanup consciente del estado: no destruir una TN sana. Si TNLCM la
            # reporta como 'created'/'activated', pudo desplegarse aunque nuestro
            # código no lo detectara; se conserva para reconciliarla con un re-POST.
            # Solo se destruye si está en estado terminal/parcial o ya no existe.
            try:
                tn_state = await tnlcm.get_tn_state(tn_id)
            except Exception as state_error:
                logger.warning(
                    f"[{execution_id}] Could not read TN {tn_id} state before cleanup: {state_error}"
                )
                tn_state = None

            if tn_state in (tnlcm.TN_STATE_CREATED | tnlcm.TN_STATE_ACTIVATED):
                logger.info(
                    f"[{execution_id}] TN {tn_id} is '{tn_state}' (healthy); skipping "
                    "destroy so it can be reconciled with a re-POST."
                )
            else:
                try:
                    await tnlcm.destroy_trial_network(tn_id, execution_id=execution_id)
                except Exception as cleanup_error:
                    logger.warning(
                        f"[{execution_id}] TN cleanup after TNLCM failure failed: {cleanup_error}"
                    )
    finally:
        state.release_tnlcm_deploy_slot(execution_id)
        # La VPN ya esta resuelta (arriba, MANUAL_REQUIRED o fallo de TNLCM):
        # desbloquea a quien este esperando en POST /executions.
        state.signal_phase(execution_id, "_vpn_ready")


async def _ensure_tunnel_up(execution_id: str) -> None:
    """Reabre el tunel WireGuard de una TN recuperada, si hace falta.

    Un teardown que llego a bajar el tunel deja la TN inalcanzable aunque siga
    viva en TNLCM. Si no se puede reabrir NO se aborta: se marca
    MANUAL_REQUIRED, igual que en el despliegue, y el experimento sigue siendo
    lanzable con el tunel puesto a mano.
    """
    record = state.executions[execution_id]
    if record.vpn_status == "UP":
        return

    conf_path = record.vpn_conf_path
    interface = record.vpn_interface or record.tn_id
    if not interface or not conf_path or not Path(conf_path).exists():
        state.update(
            execution_id,
            vpn_status="MANUAL_REQUIRED",
            vpn_error="WireGuard config is no longer available; bring the tunnel up manually",
        )
        return

    try:
        # A un hilo: `up_tunnel` lanza subprocesos bloqueantes (§8.1).
        await asyncio.to_thread(wireguard.up_tunnel, interface, conf_path)
    except wireguard.WireGuardError as exc:
        logger.warning(
            "[%s] Could not reopen WireGuard tunnel %s: %s", execution_id, interface, exc
        )
        state.update(execution_id, vpn_status="MANUAL_REQUIRED", vpn_error=str(exc))
        return

    state.update(execution_id, vpn_status="UP", vpn_error=None)


async def _reconcile_live_tn(execution_id: str) -> None:
    """Devuelve el record a TN_READY si TNLCM confirma que la TN sigue viva.

    La fuente de verdad es TNLCM, no el record. Sin esto, un record en DESTROYED
    o FAILED cuya TN nunca llego a borrarse bloqueaba /elcm con un 409 para
    siempre, y el unico camino para corregir el body era re-POSTear /executions
    (que si reconcilia por su cuenta, en `tnlcm.deploy_trial_network`).

    No hace nada si TNLCM no responde o si la TN ya no esta viva: en ese caso
    `_begin_experiment` rechaza la peticion como hasta ahora.
    """
    record = state.executions.get(execution_id)
    if record is None or not record.tn_id or record.status not in state.RECONCILABLE_STATES:
        return

    tn_id = record.tn_id
    try:
        tn_state = await tnlcm.get_tn_state(tn_id)
    except (httpx.HTTPError, ValueError) as exc:
        # Sin respuesta de TNLCM (o sin token) no se puede afirmar que la TN
        # siga viva: se deja el record como esta.
        logger.warning("[%s] Could not read TN %s state before ELCM: %s", execution_id, tn_id, exc)
        return

    if tn_state not in (tnlcm.TN_STATE_CREATED | tnlcm.TN_STATE_ACTIVATED):
        logger.info(
            "[%s] TN %s is '%s' in TNLCM: nothing to recover", execution_id, tn_id, tn_state
        )
        return

    logger.info(
        "[%s] TN %s is still '%s' in TNLCM: recovering record from %s",
        execution_id,
        tn_id,
        tn_state,
        record.status.value,
    )
    telemetry.increment_counter(
        "tn_reconcile_total", labels={"service": "orchestrator", "status": "recovered"}
    )
    await _ensure_tunnel_up(execution_id)
    state.update(
        execution_id,
        status=ExecutionState.tn_ready,
        error=None,
        message=f"TN {tn_id} is still alive in TNLCM ('{tn_state}'); recovered for new experiments",
    )
