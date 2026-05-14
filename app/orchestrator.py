import asyncio
import json
import logging
import threading
import time
from pathlib import Path

from app.config import settings
from app.models import DatasetDescriptor, ExecutionRecord, ExecutionState
from app.utils.telemetry import telemetry

logger = logging.getLogger(__name__)


class TnlcmDeploymentInProgressError(RuntimeError):
    """Raised when a TNLCM deployment is already running."""


# ELCM phase timing constants (kept local to orchestration flow)
ELCM_POLL_INTERVAL_SECONDS = 10
ELCM_EXECUTION_TIMEOUT_SECONDS = 3600
ELCM_START_TIMEOUT_SECONDS = 300

# In-memory state for MVP
executions: dict[str, ExecutionRecord] = {}
execution_descriptors: dict[str, DatasetDescriptor] = {}
elcm_start_watchdogs: dict[str, asyncio.Task[None]] = {}
_tnlcm_deploy_guard = threading.Lock()
_tnlcm_deploy_in_progress: str | None = None

# Persistencia en archivos
EXECUTIONS_FILE = Path(settings.executions_file)
EXECUTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)


def _save_executions_to_disk() -> None:
    """Guarda el estado de las ejecuciones a disco en JSON."""
    try:
        data = {execution_id: record.model_dump() for execution_id, record in executions.items()}
        with open(EXECUTIONS_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as exc:
        logger.warning(f"Could not save executions to disk: {exc}")


def _load_executions_from_disk() -> None:
    """Carga el estado de las ejecuciones desde disco al iniciar."""
    if not EXECUTIONS_FILE.exists():
        return

    try:
        with open(EXECUTIONS_FILE, "r") as f:
            data = json.load(f)

        for execution_id, record_dict in data.items():
            try:
                record = ExecutionRecord(**record_dict)
                executions[execution_id] = record
                logger.info(f"Loaded execution from disk: {execution_id}")
            except Exception as exc:
                logger.warning(f"Could not load execution {execution_id}: {exc}")
    except Exception as exc:
        logger.warning(f"Could not load executions from disk: {exc}")


# Cargar ejecuciones al iniciar el módulo
_load_executions_from_disk()


def get_execution(execution_id: str) -> ExecutionRecord | None:
    return executions.get(execution_id)


def _update(execution_id: str, **kwargs) -> None:
    record = executions[execution_id]
    old_status = record.status
    for key, value in kwargs.items():
        setattr(record, key, value)

    new_status = record.status
    if "status" in kwargs and new_status != old_status:
        message = kwargs.get("message", record.message)
        logger.debug(
            "[%s] STATUS %s -> %s | %s",
            execution_id,
            old_status.value,
            new_status.value,
            message,
        )

    _save_executions_to_disk()  # Guarda cambios inmediatamente


async def _persist_telemetry_report_best_effort(execution_id: str, stage: str) -> str | None:
    """Persist telemetry report without interrupting orchestration on I/O failures."""
    from .artifacts import build_telemetry_report_artifact

    if not settings.telemetry_report_artifacts:
        return None

    try:
        telemetry_path = await build_telemetry_report_artifact(execution_id, stage)
    except Exception as exc:
        logger.warning(
            "[%s] Could not persist telemetry report for stage %s: %s",
            execution_id,
            stage,
            exc,
        )
        return None

    record = executions.get(execution_id)
    if record:
        merged_artifacts = list(dict.fromkeys([*record.artifacts, telemetry_path]))
        _update(execution_id, artifacts=merged_artifacts)
    return telemetry_path


def _get_testcases(descriptor: DatasetDescriptor) -> list[str]:
    ordered = descriptor.experiment.testcase_paths

    unique: list[str] = []
    seen: set[str] = set()
    for tc in ordered:
        if tc in seen:
            continue
        seen.add(tc)
        unique.append(tc)
    return unique


def _cancel_elcm_start_timeout(execution_id: str) -> None:
    task = elcm_start_watchdogs.pop(execution_id, None)
    if task and not task.done():
        task.cancel()


def _acquire_tnlcm_deploy_slot(execution_id: str) -> None:
    global _tnlcm_deploy_in_progress
    with _tnlcm_deploy_guard:
        if _tnlcm_deploy_in_progress is not None:
            raise TnlcmDeploymentInProgressError(
                "Ya existe un despliegue/activacion TNLCM en curso. "
                "Espere a que termine antes de lanzar otra peticion."
            )
        _tnlcm_deploy_in_progress = execution_id


def _release_tnlcm_deploy_slot(execution_id: str) -> None:
    global _tnlcm_deploy_in_progress
    with _tnlcm_deploy_guard:
        if _tnlcm_deploy_in_progress == execution_id:
            _tnlcm_deploy_in_progress = None
            try:
                telemetry.change_gauge(
                    "active_executions", -1.0, labels={"service": "orchestrator"}
                )
            except Exception:
                pass


def _schedule_elcm_start_timeout(execution_id: str) -> None:
    _cancel_elcm_start_timeout(execution_id)
    timeout_seconds = ELCM_START_TIMEOUT_SECONDS
    elcm_start_watchdogs[execution_id] = asyncio.create_task(
        _elcm_start_timeout_watchdog(execution_id, timeout_seconds)
    )
    logger.info(
        "[%s] ELCM trigger timeout watchdog scheduled: %ss",
        execution_id,
        timeout_seconds,
    )


async def _elcm_start_timeout_watchdog(execution_id: str, timeout_seconds: int) -> None:
    try:
        await asyncio.sleep(timeout_seconds)
        await _handle_elcm_start_timeout(execution_id, timeout_seconds)
    except asyncio.CancelledError:
        logger.debug("[%s] ELCM trigger timeout watchdog cancelled", execution_id)
        raise
    finally:
        task = elcm_start_watchdogs.get(execution_id)
        if task is asyncio.current_task():
            if execution_id in elcm_start_watchdogs:
                del elcm_start_watchdogs[execution_id]


async def _handle_elcm_start_timeout(execution_id: str, timeout_seconds: int) -> None:
    from .tnlcm import destroy_trial_network
    from .utils.wireguard import down_tunnel

    record = executions.get(execution_id)
    if not record or record.status != ExecutionState.completed or not record.tn_id:
        return

    timeout_error = (
        f"ELCM phase was not triggered within {timeout_seconds} seconds after TN deployment"
    )
    logger.warning("[%s] %s. Starting automatic cleanup.", execution_id, timeout_error)

    vpn_interface = record.vpn_interface or record.tn_id
    if vpn_interface and record.vpn_status in {"UP", "DOWN_ERROR"}:
        try:
            down_tunnel(vpn_interface, record.vpn_conf_path)
            _update(execution_id, vpn_status="DOWN", vpn_error=None)
        except Exception as vpn_error:
            logger.error("[%s] WireGuard timeout cleanup failed: %s", execution_id, vpn_error)
            _update(execution_id, vpn_status="DOWN_ERROR", vpn_error=str(vpn_error))

    cleanup_message = "ELCM trigger timeout exceeded; TN was destroyed/purged automatically."
    try:
        await destroy_trial_network(record.tn_id)
    except Exception as cleanup_error:
        logger.warning("[%s] Timeout cleanup failed: %s", execution_id, cleanup_error)
        cleanup_message = f"{cleanup_message} Cleanup warning: {cleanup_error}"

    _update(
        execution_id,
        status=ExecutionState.cancelled,
        error=timeout_error,
        message=cleanup_message,
    )


async def run_tnlcm_phase(execution_id: str, descriptor: DatasetDescriptor) -> None:
    """Phase 1: validate + deploy TN, then activate the WireGuard tunnel automatically."""
    from .artifacts import build_tnlcm_raw_report_artifact, build_tnlcm_summary_artifact
    from .tnlcm import (
        deploy_trial_network,
        download_trial_network_report,
        extract_elcm_url_from_report,
        summarize_trial_network_report,
    )
    from .utils.wireguard import WireGuardManualDeploymentRequired, up_tunnel, write_tunnel_conf

    tn_id: str | None = None
    vpn_conf_path: str | None = None
    _cancel_elcm_start_timeout(execution_id)

    # Timer para TNLCM total
    tnlcm_phase_timer = telemetry.start_timer(
        "orchestrator", "tnlcm_phase", execution_id=execution_id
    )
    tnlcm_phase_timer.start()

    try:
        _update(execution_id, status=ExecutionState.validating, message="Validating descriptor")
        await asyncio.sleep(1)

        if descriptor.dataset.output != "logs":
            raise ValueError("Only dataset.output='logs' is supported")

        _update(execution_id, status=ExecutionState.deploying, message="Deploying Trial Network")
        logger.info(f"[{execution_id}] Deploying TN: {descriptor.infrastructure.name}")

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

        tn_id = await deploy_trial_network(descriptor.infrastructure, execution_id=execution_id)

        tnlcm_create_timer.stop(status="success")
        telemetry.log_event(
            "info",
            "tnlcm.create.completed",
            service="orchestrator",
            operation="tnlcm_create",
            execution_id=execution_id,
        )
        telemetry.increment_counter("tnlcm_create_total", labels={"service": "orchestrator"})

        _update(execution_id, status=ExecutionState.collecting, message="Downloading TNLCM report")
        report_markdown = download_trial_network_report(tn_id)
        raw_report_path = await build_tnlcm_raw_report_artifact(execution_id, report_markdown)
        report_markdown_from_file = Path(raw_report_path).read_text(encoding="utf-8")
        report_summary = summarize_trial_network_report(report_markdown_from_file)
        summary_report_path = await build_tnlcm_summary_artifact(
            execution_id, tn_id, report_summary
        )
        report_artifacts = [raw_report_path, summary_report_path]

        tn_init_summary = report_summary
        wireguard_config = None
        if isinstance(tn_init_summary, dict):
            wireguard_config = tn_init_summary.get("wireguard_client_config")
        if not isinstance(wireguard_config, str) or not wireguard_config.strip():
            raise ValueError("TNLCM report does not include wireguard_client_config")
        vpn_conf_path = write_tunnel_conf(execution_id, tn_id, wireguard_config)
        _update(
            execution_id,
            vpn_interface=tn_id,
            vpn_conf_path=vpn_conf_path,
            vpn_status="CONFIG_WRITTEN",
            vpn_error=None,
        )

        # Extract ELCM URL from report if available
        elcm_url = extract_elcm_url_from_report(report_summary)
        if not elcm_url:
            raise ValueError("TNLCM report does not include a valid ELCM backend URL")
        _update(execution_id, elcm_base_url=elcm_url)
        logger.info(f"[{execution_id}] ELCM URL extracted from report: {elcm_url}")

        try:
            up_tunnel(tn_id, vpn_conf_path)
        except WireGuardManualDeploymentRequired as vpn_error:
            manual_message = (
                "TN deployment completed, but WireGuard VPN could not be deployed automatically; "
                "deploy it manually before starting ELCM"
            )
            logger.warning(f"[{execution_id}] {manual_message}: {vpn_error}")
            _update(
                execution_id,
                status=ExecutionState.completed,
                tn_id=tn_id,
                artifacts=report_artifacts,
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
            await _persist_telemetry_report_best_effort(execution_id, "tnlcm_manual_required")
            _schedule_elcm_start_timeout(execution_id)
            return

        # Wait 1 second for WireGuard VPN to be fully activated before calling other components
        await asyncio.sleep(1)

        _update(execution_id, vpn_status="UP", message="TN ready and WireGuard tunnel active")

        _update(
            execution_id,
            status=ExecutionState.completed,
            tn_id=tn_id,
            artifacts=report_artifacts,
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
        await _persist_telemetry_report_best_effort(execution_id, "tnlcm_completed")

        # Auto-start ELCM if configured
        if descriptor.auto_start_elcm:
            _cancel_elcm_start_timeout(execution_id)
            logger.info(f"[{execution_id}] Auto-starting ELCM phase")
            asyncio.create_task(run_elcm_phase(execution_id))
        else:
            _schedule_elcm_start_timeout(execution_id)

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
        _update(execution_id, status=ExecutionState.failed, error=str(exc), message=f"Error: {exc}")
        await _persist_telemetry_report_best_effort(execution_id, "tnlcm_failed")
        if tn_id:
            try:
                if vpn_conf_path:
                    from .utils.wireguard import down_tunnel

                    down_tunnel(tn_id, vpn_conf_path)
            except Exception as cleanup_error:
                logger.warning(
                    f"[{execution_id}] WireGuard cleanup after TNLCM failure failed: {cleanup_error}"
                )

            try:
                from .tnlcm import destroy_trial_network

                await destroy_trial_network(tn_id)
            except Exception as cleanup_error:
                logger.warning(
                    f"[{execution_id}] TN cleanup after TNLCM failure failed: {cleanup_error}"
                )
    finally:
        _release_tnlcm_deploy_slot(execution_id)


async def run_elcm_phase(execution_id: str) -> None:
    """Phase 2: run ELCM and always cleanup TN at the end."""
    from .artifacts import build_artifacts
    from .elcm import (
        TnLogsNotFoundError,
        collect_results,
        get_experiment_status,
        run_experiment,
        upload_test_cases,
    )
    from .tnlcm import destroy_trial_network, get_tn_status
    from .utils.wireguard import down_tunnel

    record = executions[execution_id]
    descriptor = execution_descriptors[execution_id]
    tn_id = record.tn_id
    elcm_base_url = record.elcm_base_url

    if not tn_id:
        _update(execution_id, status=ExecutionState.failed, message="tn_id missing for ELCM phase")
        return
    if not elcm_base_url:
        _update(
            execution_id,
            status=ExecutionState.failed,
            message="elcm_base_url missing for ELCM phase",
        )
        return

    # Timer para ELCM total
    elcm_phase_timer = telemetry.start_timer(
        "orchestrator", "elcm_phase", execution_id=execution_id
    )
    elcm_phase_timer.start()
    telemetry.log_event(
        "info",
        "elcm.phase.started",
        service="orchestrator",
        operation="elcm_phase",
        execution_id=execution_id,
    )

    try:
        _update(
            execution_id, status=ExecutionState.running_experiment, message="Running experiments"
        )

        testcase_list = _get_testcases(descriptor)
        if not testcase_list:
            raise ValueError("At least one testcase is required")

        for index, testcase in enumerate(testcase_list, start=1):
            _update(
                execution_id,
                message=f"Uploading testcase {index}/{len(testcase_list)}: {testcase}",
            )
            await upload_test_cases(
                [testcase], elcm_base_url=elcm_base_url, execution_id=execution_id
            )

        _update(execution_id, message="Launching Exp_Desc.json")
        elcm_execution_id = await run_experiment(
            descriptor.experiment, elcm_base_url=elcm_base_url, execution_id=execution_id
        )
        experiment_ids = [elcm_execution_id]
        _update(execution_id, elcm_execution_id=elcm_execution_id)

        # Poll until terminal status using configurable ELCM timing.
        exp_done = False
        timeout_seconds = ELCM_EXECUTION_TIMEOUT_SECONDS
        poll_interval_seconds = ELCM_POLL_INTERVAL_SECONDS
        elapsed = 0

        while elapsed < timeout_seconds:
            exp_status = await get_experiment_status(
                elcm_execution_id, elcm_base_url=elcm_base_url, execution_id=execution_id
            )
            logger.info(f"ELCM execution {elcm_execution_id} status: {exp_status}")

            # Check if execution is finished
            if "Finished" in exp_status or exp_status.upper() in {"FINISHED", "COMPLETED", "DONE"}:
                exp_done = True
                break

            # Check for errors
            if "Error" in exp_status or "FAILED" in exp_status.upper():
                raise RuntimeError(
                    f"ELCM execution {elcm_execution_id} failed with status: {exp_status}"
                )

            await asyncio.sleep(poll_interval_seconds)
            elapsed += poll_interval_seconds

        if not exp_done:
            raise TimeoutError(f"Timeout waiting for ELCM execution {elcm_execution_id} to finish")

        # Collect logs with transient error handling
        _update(execution_id, status=ExecutionState.collecting, message="Collecting logs")
        try:
            execution_logs = await collect_results(
                elcm_execution_id, elcm_base_url=elcm_base_url, execution_id=execution_id
            )
        except TnLogsNotFoundError as logs_error:
            logger.warning(f"ELCM logs not found for {elcm_execution_id}: {logs_error}")
            raise
        except Exception as logs_error:
            # If logs error, check TN status before failing
            logger.warning(f"Error collecting logs for {elcm_execution_id}: {logs_error}")
            tn_status = get_tn_status(tn_id)
            logger.info(f"TN {tn_id} status after logs error: {tn_status}")

            # If TN is running, logs will be available later, return empty for now
            if "RUNNING" in tn_status.upper() or "ACTIVE" in tn_status.upper():
                logger.info("TN still running, treating logs as pending")
                execution_logs = {
                    "output": "logs",
                    "experiment_id": elcm_execution_id,
                    "logs": {"message": "Logs not available yet"},
                    "status": "logs_pending",
                }
            else:
                # TN is in error state, re-raise the error
                raise

        _update(
            execution_id,
            experiment_id=elcm_execution_id,
            experiment_ids=experiment_ids,
            message="Experiment finished",
        )

        results = {
            "output": "logs",
            "experiment_ids": experiment_ids,
            "testcases": testcase_list,
            "logs": execution_logs,
        }
        artifact_paths = await build_artifacts(execution_id, tn_id, elcm_execution_id, results)
        merged_artifacts = list(dict.fromkeys([*record.artifacts, *artifact_paths]))
        _update(
            execution_id,
            status=ExecutionState.completed,
            artifacts=merged_artifacts,
            message="ELCM phase completed. TN cleanup done.",
        )
        elcm_phase_timer.stop(status="success")
        telemetry.log_event(
            "info",
            "elcm.phase.completed",
            service="orchestrator",
            operation="elcm_phase",
            execution_id=execution_id,
            status="success",
        )
        telemetry.increment_counter(
            "elcm_phase_total", labels={"service": "orchestrator", "status": "success"}
        )

    except Exception as exc:
        logger.error(f"[{execution_id}] ELCM phase error: {exc}")
        elcm_phase_timer.stop(status="error")
        telemetry.log_event(
            "error",
            "elcm.phase.failed",
            service="orchestrator",
            operation="elcm_phase",
            execution_id=execution_id,
            error=str(exc),
        )
        telemetry.increment_counter(
            "errors_total", labels={"service": "orchestrator", "operation": "elcm_phase"}
        )
        _update(execution_id, status=ExecutionState.failed, error=str(exc), message=f"Error: {exc}")

    finally:
        _cancel_elcm_start_timeout(execution_id)
        vpn_interface = record.vpn_interface or tn_id
        if vpn_interface:
            try:
                logger.info(
                    f"[{execution_id}] Cleanup: deactivating WireGuard tunnel {vpn_interface}"
                )
                down_tunnel(vpn_interface, record.vpn_conf_path)
                _update(execution_id, vpn_status="DOWN", vpn_error=None)
            except Exception as vpn_error:
                logger.error(f"[{execution_id}] WireGuard deactivation failed: {vpn_error}")
                _update(execution_id, vpn_status="DOWN_ERROR", vpn_error=str(vpn_error))

        try:
            logger.info(f"[{execution_id}] Cleanup: destroying TN {tn_id}")
            await destroy_trial_network(tn_id)
        except Exception as cleanup_error:
            logger.warning(f"[{execution_id}] Cleanup failed: {cleanup_error}")

        final_record = executions.get(execution_id)
        final_stage = (
            "elcm_completed"
            if final_record and final_record.status == ExecutionState.completed
            else "elcm_finalized"
        )
        await _persist_telemetry_report_best_effort(execution_id, final_stage)
        logger.info("[%s] ELCM phase finalization completed", execution_id)

        # Cerrar timer global de ejecución
        execution_timer = getattr(final_record, "_execution_timer", None)
        if execution_timer is not None:
            final_status = (
                "success"
                if final_record and final_record.status == ExecutionState.completed
                else "failed"
            )
            execution_timer.stop(status=final_status)
            telemetry.log_event(
                "info",
                "orchestrator_execution.completed",
                service="orchestrator",
                operation="create",
                execution_id=execution_id,
                status=final_status,
            )
            telemetry.increment_counter(
                "orchestrator_execution_total",
                labels={"service": "orchestrator", "status": final_status},
            )


async def create_tnlcm_execution(descriptor: DatasetDescriptor) -> ExecutionRecord:
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
    _acquire_tnlcm_deploy_slot(execution_id)
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
            from app.utils.telemetry import format_duration_display

            payload["duration_display"] = format_duration_display(lock_wait)
        telemetry.log_event("info", "tnlcm_lock.acquire.completed", **payload)
    except Exception:
        pass

    _cancel_elcm_start_timeout(execution_id)
    try:
        record = ExecutionRecord(
            execution_id=execution_id,
            status=ExecutionState.pending,
            message="Execution created",
        )
        executions[execution_id] = record
        execution_descriptors[execution_id] = descriptor
        logger.debug(
            "[%s] STATUS NONE -> %s | %s", execution_id, record.status.value, record.message
        )
        _save_executions_to_disk()  # Guarda al crear

        # Update telemetry: active executions gauge
        try:
            telemetry.change_gauge("active_executions", 1.0, labels={"service": "orchestrator"})
        except Exception:
            pass

        # Store execution timer for later closure at end of orchestration
        setattr(executions[execution_id], "_execution_timer", execution_timer)

        asyncio.create_task(run_tnlcm_phase(execution_id, descriptor))
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
        _release_tnlcm_deploy_slot(execution_id)
        raise


async def start_elcm_phase(execution_id: str) -> ExecutionRecord:
    record = get_execution(execution_id)
    if not record:
        raise ValueError("Execution not found")
    if execution_id not in execution_descriptors:
        raise ValueError("Descriptor not found for execution")
    if not record.tn_id:
        raise ValueError("TNLCM phase is not ready yet (tn_id missing)")
    if record.status != ExecutionState.completed:
        raise ValueError("ELCM phase can only be started when TNLCM phase is COMPLETED")

    _cancel_elcm_start_timeout(execution_id)
    _update(execution_id, status=ExecutionState.running_experiment, message="ELCM phase triggered")
    asyncio.create_task(run_elcm_phase(execution_id))
    return executions[execution_id]
