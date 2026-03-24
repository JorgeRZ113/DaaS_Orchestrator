import asyncio
import json
import logging
from pathlib import Path

from app.config import settings
from app.models import DatasetDescriptor, ExecutionRecord, ExecutionState

logger = logging.getLogger(__name__)

# In-memory state for MVP
executions: dict[str, ExecutionRecord] = {}
execution_descriptors: dict[str, DatasetDescriptor] = {}

# Persistencia en archivos
EXECUTIONS_FILE = Path(settings.executions_file)
EXECUTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)


def _save_executions_to_disk() -> None:
    """Guarda el estado de las ejecuciones a disco en JSON."""
    try:
        data = {
            execution_id: record.dict()
            for execution_id, record in executions.items()
        }
        with open(EXECUTIONS_FILE, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as exc:
        logger.warning(f"Could not save executions to disk: {exc}")


def _load_executions_from_disk() -> None:
    """Carga el estado de las ejecuciones desde disco al iniciar."""
    if not EXECUTIONS_FILE.exists():
        return
    
    try:
        with open(EXECUTIONS_FILE, 'r') as f:
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
    for key, value in kwargs.items():
        setattr(record, key, value)
    _save_executions_to_disk()  # Guarda cambios inmediatamente


def _get_testcases(descriptor: DatasetDescriptor) -> list[str]:
    ordered: list[str] = []
    if descriptor.experiment.testcase_path:
        ordered.append(descriptor.experiment.testcase_path)
    ordered.extend(descriptor.experiment.testcase_paths)

    unique: list[str] = []
    seen: set[str] = set()
    for tc in ordered:
        if tc in seen:
            continue
        seen.add(tc)
        unique.append(tc)
    return unique


async def run_tnlcm_phase(execution_id: str, descriptor: DatasetDescriptor) -> None:
    """Phase 1: validate + deploy TN. Leaves execution waiting for manual VPN step."""
    from .artifacts import build_tnlcm_report_artifacts
    from .tnlcm import (
        deploy_trial_network,
        download_trial_network_report,
        summarize_trial_network_report,
    )

    try:
        _update(execution_id, status=ExecutionState.validating, message="Validating descriptor")
        await asyncio.sleep(1)

        if descriptor.dataset.output != "logs":
            raise ValueError("Only dataset.output='logs' is supported")

        _update(execution_id, status=ExecutionState.deploying, message="Deploying Trial Network")
        logger.info(f"[{execution_id}] Deploying TN: {descriptor.infrastructure.name}")
        tn_id = await deploy_trial_network(descriptor.infrastructure)

        _update(execution_id, status=ExecutionState.collecting, message="Downloading TNLCM report")
        report_payload = await download_trial_network_report(tn_id)
        report_summary = summarize_trial_network_report(tn_id, report_payload)
        report_artifacts = await build_tnlcm_report_artifacts(
            execution_id,
            tn_id,
            report_payload,
            report_summary,
        )

        _update(
            execution_id,
            status=ExecutionState.completed,
            tn_id=tn_id,
            artifacts=report_artifacts,
            message=(
                "TN deployment request accepted. Activate WireGuard manually and continue with "
                "POST /executions/{execution_id}/elcm"
            ),
        )
        logger.info(f"[{execution_id}] TN {tn_id} deployment/activate acknowledged. Waiting VPN step.")

    except Exception as exc:
        logger.error(f"[{execution_id}] TNLCM phase error: {exc}")
        _update(execution_id, status=ExecutionState.failed, error=str(exc), message=f"Error: {exc}")


async def run_elcm_phase(execution_id: str) -> None:
    """Phase 2: run ELCM and always cleanup TN at the end."""
    from .artifacts import build_artifacts
    from .elcm import collect_results, get_experiment_status, run_experiment
    from .tnlcm import destroy_trial_network, get_tn_status

    record = executions[execution_id]
    descriptor = execution_descriptors[execution_id]
    tn_id = record.tn_id

    if not tn_id:
        _update(execution_id, status=ExecutionState.failed, message="tn_id missing for ELCM phase")
        return

    try:
        _update(execution_id, status=ExecutionState.running_experiment, message="Running experiments")

        testcase_list = _get_testcases(descriptor)
        if not testcase_list:
            raise ValueError("At least one testcase is required")

        success_states = {"PASS", "FAIL", "COMPLETED", "DONE"}
        error_states = {"ERROR", "CANCEL", "FAILED"}

        experiment_ids: list[str] = []
        all_logs: list[dict] = []

        for index, testcase in enumerate(testcase_list, start=1):
            _update(
                execution_id,
                message=f"Running testcase {index}/{len(testcase_list)}: {testcase}",
            )
            # run_experiment returns the ELCM execution_id
            elcm_execution_id = await run_experiment(
                descriptor.experiment,
                tn_id,
                testcase_path_override=testcase,
            )
            experiment_ids.append(elcm_execution_id)
            
            # Store ELCM execution ID
            _update(execution_id, elcm_execution_id=elcm_execution_id)

            # Poll every 10 seconds until "Finished"
            exp_done = False
            timeout_seconds = 3600  # 1 hour timeout
            elapsed = 0
            
            while elapsed < timeout_seconds:
                exp_status = await get_experiment_status(elcm_execution_id)
                logger.info(f"ELCM execution {elcm_execution_id} status: {exp_status}")
                
                # Check if execution is finished
                if "Finished" in exp_status or exp_status.upper() in {"FINISHED", "COMPLETED", "DONE"}:
                    exp_done = True
                    break
                
                # Check for errors
                if "Error" in exp_status or "FAILED" in exp_status.upper():
                    raise RuntimeError(f"ELCM execution {elcm_execution_id} failed with status: {exp_status}")
                
                await asyncio.sleep(10)  # Wait 10 seconds before next poll
                elapsed += 10

            if not exp_done:
                raise TimeoutError(f"Timeout waiting for ELCM execution {elcm_execution_id} to finish")

            # Collect logs with transient error handling
            _update(execution_id, status=ExecutionState.collecting, message="Collecting logs")
            try:
                testcase_logs = await collect_results(elcm_execution_id)
            except Exception as logs_error:
                # If logs error, check TN status before failing
                logger.warning(f"Error collecting logs for {elcm_execution_id}: {logs_error}")
                tn_status = await get_tn_status(tn_id)
                logger.info(f"TN {tn_id} status after logs error: {tn_status}")
                
                # If TN is running, logs will be available later, return empty for now
                if "RUNNING" in tn_status.upper() or "ACTIVE" in tn_status.upper():
                    logger.info(f"TN still running, treating logs as pending")
                    testcase_logs = {
                        "testcase": testcase,
                        "execution_id": elcm_execution_id,
                        "result": {"message": "Logs not available yet"},
                        "status": "logs_pending",
                    }
                else:
                    # TN is in error state, re-raise the error
                    raise
            
            all_logs.append(
                {
                    "testcase": testcase,
                    "execution_id": elcm_execution_id,
                    "result": testcase_logs,
                }
            )
            _update(execution_id, status=ExecutionState.running_experiment)

        first_experiment_id = experiment_ids[0]
        _update(
            execution_id,
            experiment_id=first_experiment_id,
            experiment_ids=experiment_ids,
            message="All testcases finished",
        )

        results = {
            "output": "logs",
            "experiment_ids": experiment_ids,
            "logs": all_logs,
        }
        artifact_paths = await build_artifacts(execution_id, tn_id, first_experiment_id, results)
        merged_artifacts = list(dict.fromkeys([*record.artifacts, *artifact_paths]))
        _update(
            execution_id,
            status=ExecutionState.completed,
            artifacts=merged_artifacts,
            message="ELCM phase completed. TN cleanup done.",
        )

    except Exception as exc:
        logger.error(f"[{execution_id}] ELCM phase error: {exc}")
        _update(execution_id, status=ExecutionState.failed, error=str(exc), message=f"Error: {exc}")

    finally:
        try:
            logger.info(f"[{execution_id}] Cleanup: destroying TN {tn_id}")
            await destroy_trial_network(tn_id)
        except Exception as cleanup_error:
            logger.warning(f"[{execution_id}] Cleanup failed: {cleanup_error}")


async def create_tnlcm_execution(descriptor: DatasetDescriptor) -> ExecutionRecord:
    execution_id = descriptor.infrastructure.name.strip()
    record = ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.pending,
        message="Execution created",
    )
    executions[execution_id] = record
    execution_descriptors[execution_id] = descriptor
    _save_executions_to_disk()  # Guarda al crear

    asyncio.create_task(run_tnlcm_phase(execution_id, descriptor))
    return record


async def start_elcm_phase(execution_id: str) -> ExecutionRecord:
    record = get_execution(execution_id)
    if not record:
        raise ValueError("Execution not found")
    if execution_id not in execution_descriptors:
        raise ValueError("Descriptor not found for execution")
    if not record.tn_id:
        raise ValueError("TNLCM phase is not ready yet (tn_id missing)")
    if record.status == ExecutionState.running_experiment:
        raise ValueError("ELCM phase is already running")

    _update(execution_id, status=ExecutionState.running_experiment, message="ELCM phase triggered")
    asyncio.create_task(run_elcm_phase(execution_id))
    return executions[execution_id]

