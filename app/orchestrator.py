import asyncio
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Coroutine

from app import artifacts, elcm, tnlcm
from app.config import settings
from app.elcm import ElcmResultsNotFoundError, TnLogsNotFoundError
from app.generators import elcm_dataset
from app.generators.tnlcm_renderer import generate_tnlcm_descriptor
from app.models import (
    DatasetDescriptor,
    DatasetRequest,
    ExecutionRecord,
    ExecutionState,
    ExperimentConfig,
    ExperimentRun,
)
from app.utils import influx_raw, results_bundle, wireguard
from app.utils.telemetry import format_duration_display, telemetry
from app.utils.wireguard import WireGuardManualDeploymentRequired

logger = logging.getLogger(__name__)


class TnlcmDeploymentInProgressError(RuntimeError):
    """Raised when a TNLCM deployment is already running."""


class ExecutionNotFoundError(LookupError):
    """Raised when the referenced execution_id does not exist."""


class ExecutionConflictError(RuntimeError):
    """Raised when the execution state does not allow the requested operation (HTTP 409)."""


# ELCM phase timing constants (kept local to orchestration flow)
ELCM_POLL_INTERVAL_SECONDS = 10
ELCM_EXECUTION_TIMEOUT_SECONDS = 3600

# Formatos de dataset.output realmente implementados en el runtime. El esquema
# (models.DatasetOutput) acepta también csv/dashboard/raw, pero se activan de
# forma incremental; pedir uno todavía no implementado aborta la ejecución
# (fail-fast). Al implementar un formato nuevo, se añade aquí.
IMPLEMENTED_DATASET_OUTPUTS: set[str] = {"logs", "csv", "dashboard", "raw"}

# Puertos estándar en la VM de monitorización (ports: 8086 Influx, 3000 Grafana,
# 9090 Prometheus). Grafana -> URL del dashboard; Influx -> consulta raw.
GRAFANA_PORT = 3000
INFLUX_PORT = 8086

# In-memory state for MVP
executions: dict[str, ExecutionRecord] = {}
_tnlcm_deploy_guard = threading.Lock()
_tnlcm_deploy_in_progress: str | None = None

# Registro de background tasks (§8.2): retiene la referencia y loguea fallos.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background_task(coro: Coroutine, *, name: str) -> asyncio.Task:
    """Lanza una task supervisada: retiene la referencia y loguea excepciones."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _on_done(finished: asyncio.Task) -> None:
        _background_tasks.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            logger.error(
                "Background task %s failed: %s",
                name,
                finished.exception(),
                exc_info=finished.exception(),
            )

    task.add_done_callback(_on_done)
    return task


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _set_experiment_run_fields(execution_id: str, name: str, **kwargs) -> None:
    """Actualiza el ExperimentRun mas reciente con ese nombre y persiste."""
    record = executions.get(execution_id)
    if not record:
        return
    for run in reversed(record.experiments):
        if run.name == name:
            for key, value in kwargs.items():
                setattr(run, key, value)
            break
    _save_executions_to_disk()


async def _persist_telemetry_report_best_effort(execution_id: str, stage: str) -> str | None:
    """Persist telemetry report without interrupting orchestration on I/O failures."""
    if not settings.telemetry_report_artifacts:
        return None

    try:
        telemetry_path = await artifacts.build_telemetry_report_artifact(execution_id, stage)
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


def _get_testcases(experiment: ExperimentConfig) -> list[str]:
    ordered = experiment.testcase_paths

    unique: list[str] = []
    seen: set[str] = set()
    for tc in ordered:
        if tc in seen:
            continue
        seen.add(tc)
        unique.append(tc)
    return unique


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
        _update(execution_id, status=ExecutionState.validating, message="Validating descriptor")
        await asyncio.sleep(1)

        unsupported = [
            fmt for fmt in descriptor.dataset.output if fmt not in IMPLEMENTED_DATASET_OUTPUTS
        ]
        if unsupported:
            raise ValueError(
                f"dataset.output not yet implemented: {', '.join(unsupported)}. "
                f"Currently supported: {', '.join(sorted(IMPLEMENTED_DATASET_OUTPUTS))}"
            )

        _update(
            execution_id, status=ExecutionState.validating, message="Generating TNLCM descriptor"
        )
        tnlcm_descriptor_path = await generate_tnlcm_descriptor(
            descriptor.infrastructure, execution_id
        )

        _update(execution_id, status=ExecutionState.deploying, message="Deploying Trial Network")
        logger.info(f"[{execution_id}] Deploying TN: {descriptor.infrastructure.name}")

        # Registrar el tn_id en cuanto se conoce (antes de desplegar): si el deploy
        # falla a mitad, el record conserva la TN direccionable para poder
        # reconciliarla con un re-POST o borrarla con el endpoint DELETE.
        tn_id = tnlcm.resolve_tn_id(descriptor.infrastructure)
        _update(execution_id, tn_id=tn_id)

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

        _update(execution_id, status=ExecutionState.collecting, message="Downloading TNLCM report")
        report_markdown = tnlcm.download_trial_network_report(tn_id)
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
        _update(
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
        _update(execution_id, elcm_base_url=elcm_url)
        logger.info(f"[{execution_id}] ELCM URL extracted from report: {elcm_url}")

        record_for_artifacts = executions[execution_id]
        merged_report_artifacts = list(
            dict.fromkeys([*record_for_artifacts.artifacts, *report_artifacts])
        )

        try:
            wireguard.up_tunnel(tn_id, vpn_conf_path)
        except WireGuardManualDeploymentRequired as vpn_error:
            manual_message = (
                "TN deployment completed, but WireGuard VPN could not be deployed automatically; "
                "deploy it manually before starting ELCM"
            )
            logger.warning(f"[{execution_id}] {manual_message}: {vpn_error}")
            _update(
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
            await _persist_telemetry_report_best_effort(execution_id, "tnlcm_manual_required")
            return

        # Wait 1 second for WireGuard VPN to be fully activated before calling other components
        await asyncio.sleep(1)

        _update(execution_id, vpn_status="UP", message="TN ready and WireGuard tunnel active")

        _update(
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
        await _persist_telemetry_report_best_effort(execution_id, "tnlcm_completed")

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
        _update(execution_id, status=ExecutionState.failed, error=str(exc), message=f"Error: {exc}")
        await _persist_telemetry_report_best_effort(execution_id, "tnlcm_failed")
        if tn_id:
            try:
                if vpn_conf_path:
                    wireguard.down_tunnel(tn_id, vpn_conf_path)
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
                    await tnlcm.destroy_trial_network(tn_id)
                except Exception as cleanup_error:
                    logger.warning(
                        f"[{execution_id}] TN cleanup after TNLCM failure failed: {cleanup_error}"
                    )
    finally:
        _release_tnlcm_deploy_slot(execution_id)


async def _collect_csv_results(
    execution_id: str,
    elcm_execution_id: str,
    elcm_base_url: str,
    experiment_name: str | None = None,
) -> list[str]:
    """Descargar el ZIP de resultados de ELCM y extraer el/los CSV en result/.

    Devuelve las rutas de los CSV extraídos (para registrarlos como artifacts).
    Si ELCM todavía no tiene resultados (404) se registra un aviso y se devuelve
    lista vacía, sin abortar la fase.
    """
    result_dir = Path(artifacts._artifact_result_dir(execution_id, experiment_name))
    result_dir.mkdir(parents=True, exist_ok=True)
    zip_path = result_dir / f"csv_results_{elcm_execution_id}.zip"

    try:
        await elcm.download_execution_results(
            elcm_execution_id,
            dest_path=str(zip_path),
            elcm_base_url=elcm_base_url,
            execution_id=execution_id,
        )
    except ElcmResultsNotFoundError as exc:
        logger.warning("[%s] No CSV results available: %s", execution_id, exc)
        return []

    # Extracción/limpieza es I/O de disco -> fuera del event loop (§8.1).
    csv_files = await asyncio.to_thread(results_bundle.extract_csv_bundle, zip_path, result_dir)

    # El ZIP externo ya no hace falta una vez extraído el CSV.
    try:
        zip_path.unlink()
    except OSError:
        pass

    logger.info("[%s] CSV dataset collected: %d file(s)", execution_id, len(csv_files))
    return [str(path) for path in csv_files]


async def _collect_dashboard_results(
    execution_id: str, elcm_execution_id: str, experiment_name: str | None = None
) -> list[str]:
    """Construir la URL del dashboard Grafana y guardarla en result/.

    URL: http://<IP_monitoring>:<GRAFANA_PORT>/d/Run<elcm_execution_id>. ELCM crea
    el dashboard con uid Run<id> al ejecutar el TestCase de grafana; la IP de
    monitorización se toma del report TNLCM persistido. No se verifica que el
    dashboard exista: solo se entrega la URL.
    """
    monitoring = artifacts.load_monitoring_info(execution_id)
    ip = monitoring.get("ip")
    if not ip:
        raise ValueError(
            f"Cannot build dashboard URL: monitoring IP missing in TNLCM report "
            f"for execution {execution_id}"
        )

    uid = f"Run{elcm_execution_id}"
    url = f"http://{ip}:{GRAFANA_PORT}/d/{uid}"

    result_dir = Path(artifacts._artifact_result_dir(execution_id, experiment_name))
    result_dir.mkdir(parents=True, exist_ok=True)
    dashboard_path = result_dir / "dashboard.json"
    dashboard_path.write_text(
        json.dumps(
            {
                "output": "dashboard",
                "url": url,
                "grafana_uid": uid,
                "elcm_execution_id": elcm_execution_id,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("[%s] Dashboard URL collected: %s", execution_id, url)
    return [str(dashboard_path)]


async def _collect_raw_results(
    execution_id: str, elcm_execution_id: str, experiment_name: str | None = None
) -> list[str]:
    """Consultar InfluxDB directamente (Flux, 2 pasos) y volcar el CSV crudo en result/.

    Réplica de la interfaz east/west de ELCM: descubre los measurements de la
    ejecución y vuelca cada uno a `raw_<measurement>.csv`. IP/token/org/bucket
    salen del bloque monitoring del report TNLCM (el token en memoria, §8.7).
    Fail-fast si falta la IP o el token de Influx.
    """
    monitoring = artifacts.load_monitoring_info(execution_id)
    ip = monitoring.get("ip")
    credentials = monitoring.get("credentials") or {}
    token = credentials.get("token")
    org = credentials.get("organization") or "testing"
    bucket = credentials.get("bucket") or "testing"

    if not ip or not token:
        raise ValueError(
            f"Cannot query raw InfluxDB data: missing monitoring ip/token in TNLCM "
            f"report for execution {execution_id}"
        )

    measurements = await influx_raw.collect_raw_measurements(
        host=ip,
        port=INFLUX_PORT,
        org=org,
        bucket=bucket,
        token=token,
        execution_id=elcm_execution_id,
    )

    result_dir = Path(artifacts._artifact_result_dir(execution_id, experiment_name))
    result_dir.mkdir(parents=True, exist_ok=True)

    raw_paths: list[str] = []
    for measurement, csv_text in measurements.items():
        # Sanear el nombre del measurement para usarlo como nombre de fichero.
        safe = re.sub(r"\W+", "_", measurement) or "measurement"
        raw_path = result_dir / f"raw_{safe}.csv"
        await asyncio.to_thread(raw_path.write_text, csv_text, "utf-8")
        raw_paths.append(str(raw_path))

    logger.info("[%s] Raw dataset collected: %d measurement(s)", execution_id, len(raw_paths))
    return raw_paths


async def run_elcm_phase(
    execution_id: str,
    experiment: ExperimentConfig,
    dataset: DatasetRequest | None = None,
    *,
    ephemeral: bool = False,
) -> None:
    """Phase 2: run one ELCM experiment over the live TN.

    La TN no se toca al terminar: queda en TN_READY para aceptar mas
    experimentos. Solo si `ephemeral=True` (TN de un solo uso) se encadena
    `run_teardown_phase` al finalizar el experimento.

    El `dataset` (formatos de salida) es POR EXPERIMENTO: llega en el body de
    /elcm y define qué se recolecta y se guarda en result/<experimento>/. Si no
    se indica, se usan los formatos fijados al crear la ejecución.
    """
    record = executions[execution_id]
    tn_id = record.tn_id
    elcm_base_url = record.elcm_base_url

    # Formatos de salida de ESTE experimento (por defecto, los de la ejecución).
    dataset_outputs = list(dataset.output) if dataset is not None else list(record.dataset_output)

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
            execution_id,
            status=ExecutionState.running_experiment,
            message=f"Running experiment '{experiment.name}'",
        )

        # Fail-fast: rechazar formatos de salida aún no implementados en runtime.
        unsupported = [fmt for fmt in dataset_outputs if fmt not in IMPLEMENTED_DATASET_OUTPUTS]
        if unsupported:
            raise ValueError(
                f"dataset.output not yet implemented: {', '.join(unsupported)}. "
                f"Currently supported: {', '.join(sorted(IMPLEMENTED_DATASET_OUTPUTS))}"
            )

        # Deja constancia en el ExperimentRun de los formatos realmente usados.
        _set_experiment_run_fields(
            execution_id, experiment.name, dataset_output=list(dataset_outputs)
        )

        testcase_list = _get_testcases(experiment)
        if not testcase_list:
            raise ValueError("At least one testcase is required")

        # Los TestCases del body se resuelven a su fichero real (examples/) y se
        # suben TAL CUAL: no se re-renderizan (eso corrompía el entrecomillado y
        # la indentación). Fail-fast si alguno no existe.
        generated_testcase_paths: list[str] = [
            str(elcm.resolve_testcase_file(testcase_ref)) for testcase_ref in testcase_list
        ]
        # Ficheros del usuario antes de inyectar los TC de dataset: de aqui se lee el
        # TestCase de captura (*_capture*) para el dashboard.
        user_testcase_files = list(generated_testcase_paths)

        # Inyección de TestCases de dataset (csv/dashboard): se generan con ytt y
        # se añaden a la lista de TestCases (upload + descriptor) para que ELCM
        # los ejecute y produzca el CSV / cree el dashboard. Se guardan como
        # <Name>.yml. (raw NO inyecta TestCase: consultará InfluxDB directamente.)
        for kind in ("csv", "dashboard"):
            if kind in dataset_outputs and kind in elcm_dataset.ELCM_DATASET_TEMPLATES:
                data_values = None
                if kind == "dashboard":
                    # El dashboard se genera con un panel por metrica; measurement y
                    # metricas salen del TestCase de captura (*_capture*) del body.
                    capture = elcm.extract_capture_metrics(user_testcase_files)
                    if capture is None:
                        raise ValueError(
                            "dataset.output 'dashboard' requiere un TestCase de captura "
                            "(*_capture* con Run.PrometheusToInflux) en testcase_paths"
                        )
                    measurement, metrics = capture
                    data_values = {"dataset": {"measurement": measurement, "metrics": metrics}}
                dataset_tc_path = await elcm_dataset.generate_elcm_dataset_testcase(
                    kind, execution_id, data_values=data_values
                )
                generated_testcase_paths.append(str(dataset_tc_path))
                logger.info("[%s] Injected %s dataset testcase into experiment", execution_id, kind)

        _update(execution_id, message="Generating Experiment Descriptor")
        experiment_descriptor_path = await elcm.generate_experiment_descriptor(
            experiment,
            generated_testcase_paths,
            execution_id,
        )

        _update(execution_id, message="Uploading TestCases")
        await elcm.upload_test_cases(
            generated_testcase_paths, elcm_base_url=elcm_base_url, execution_id=execution_id
        )

        _update(execution_id, message="Launching experiment descriptor")
        elcm_execution_id = await elcm.run_experiment(
            experiment,
            elcm_base_url=elcm_base_url,
            execution_id=execution_id,
            exp_descriptor_path=experiment_descriptor_path,
        )
        experiment_ids = list(dict.fromkeys([*record.experiment_ids, elcm_execution_id]))
        _update(execution_id, elcm_execution_id=elcm_execution_id)
        _set_experiment_run_fields(
            execution_id, experiment.name, elcm_execution_id=elcm_execution_id
        )

        # Poll until terminal status using configurable ELCM timing.
        exp_done = False
        timeout_seconds = ELCM_EXECUTION_TIMEOUT_SECONDS
        poll_interval_seconds = ELCM_POLL_INTERVAL_SECONDS
        elapsed = 0

        while elapsed < timeout_seconds:
            exp_status = await elcm.get_experiment_status(
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

        # --- Recolección de outputs del dataset (según dataset.output) ---
        _update(execution_id, status=ExecutionState.collecting, message="Collecting dataset")
        collected_artifacts: list[str] = []

        # logs: comportamiento previo, ahora gated por dataset.output. La
        # recolección de logs actúa además como verificación de que el
        # experimento se ejecutó (TnLogsNotFoundError).
        if "logs" in dataset_outputs:
            try:
                execution_logs = await elcm.collect_results(
                    elcm_execution_id, elcm_base_url=elcm_base_url, execution_id=execution_id
                )
            except TnLogsNotFoundError as logs_error:
                logger.warning(f"ELCM logs not found for {elcm_execution_id}: {logs_error}")
                raise
            except Exception as logs_error:
                # If logs error, check TN status before failing
                logger.warning(f"Error collecting logs for {elcm_execution_id}: {logs_error}")
                tn_status = tnlcm.get_tn_status(tn_id)
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

            results = {
                "output": "logs",
                "experiment_ids": experiment_ids,
                "testcases": testcase_list,
                "logs": execution_logs,
            }
            collected_artifacts.extend(
                await artifacts.build_artifacts(
                    execution_id,
                    tn_id,
                    elcm_execution_id,
                    results,
                    experiment_name=experiment.name,
                )
            )

        # csv: descargar el ZIP de resultados de ELCM y extraer el/los CSV.
        if "csv" in dataset_outputs:
            collected_artifacts.extend(
                await _collect_csv_results(
                    execution_id, elcm_execution_id, elcm_base_url, experiment.name
                )
            )

        # dashboard: construir y guardar la URL del dashboard Grafana.
        if "dashboard" in dataset_outputs:
            collected_artifacts.extend(
                await _collect_dashboard_results(execution_id, elcm_execution_id, experiment.name)
            )

        # raw: consultar InfluxDB directamente y volcar el CSV crudo por measurement.
        if "raw" in dataset_outputs:
            collected_artifacts.extend(
                await _collect_raw_results(execution_id, elcm_execution_id, experiment.name)
            )

        _update(
            execution_id,
            experiment_id=elcm_execution_id,
            experiment_ids=experiment_ids,
            message="Experiment finished",
        )

        merged_artifacts = list(
            dict.fromkeys(
                [
                    *record.artifacts,
                    *generated_testcase_paths,
                    experiment_descriptor_path,
                    *collected_artifacts,
                ]
            )
        )
        _set_experiment_run_fields(
            execution_id, experiment.name, status="FINISHED", finished_at=_utc_now_iso()
        )
        _update(
            execution_id,
            status=ExecutionState.tn_ready,
            artifacts=merged_artifacts,
            message=f"Experiment '{experiment.name}' finished. TN still alive.",
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
        _set_experiment_run_fields(
            execution_id,
            experiment.name,
            status="FAILED",
            error=str(exc),
            finished_at=_utc_now_iso(),
        )
        # La TN sigue viva: se vuelve a TN_READY para permitir reintentar otro
        # experimento o lanzar el borrado manual. El error queda registrado.
        _update(
            execution_id,
            status=ExecutionState.tn_ready,
            error=str(exc),
            message=f"Experiment '{experiment.name}' failed: {exc}. TN still alive.",
        )

    finally:
        final_record = executions.get(execution_id)
        final_stage = (
            "elcm_completed"
            if final_record and final_record.status == ExecutionState.tn_ready
            else "elcm_finalized"
        )
        await _persist_telemetry_report_best_effort(execution_id, final_stage)
        logger.info("[%s] ELCM phase finalization completed", execution_id)

        # TN de un solo uso: encadenar el bloque de borrado tras el experimento
        # automatico, tanto si termino bien como si fallo.
        if ephemeral:
            logger.info("[%s] ephemeral_tn=true: chaining TN teardown", execution_id)
            await run_teardown_phase(execution_id)


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
            payload["duration_display"] = format_duration_display(lock_wait)
        telemetry.log_event("info", "tnlcm_lock.acquire.completed", **payload)
    except Exception:
        pass

    try:
        descriptor_path = artifacts.persist_dataset_descriptor(execution_id, descriptor)

        record = ExecutionRecord(
            execution_id=execution_id,
            status=ExecutionState.pending,
            message="Execution created",
            # ephemeral_tn solo aplica con auto-start; en manual se ignora
            ephemeral_tn=descriptor.auto_start_elcm and descriptor.ephemeral_tn,
            dataset_output=list(descriptor.dataset.output),
            artifacts=[descriptor_path],
        )
        executions[execution_id] = record
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

        _spawn_background_task(
            run_tnlcm_phase(execution_id, descriptor), name=f"tnlcm:{execution_id}"
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
        _release_tnlcm_deploy_slot(execution_id)
        raise


def _begin_experiment(
    execution_id: str,
    experiment: ExperimentConfig,
    dataset: DatasetRequest,
    *,
    ephemeral: bool = False,
) -> ExecutionRecord:
    """Valida y arranca un experimento sobre la TN viva.

    La transicion TN_READY -> RUNNING_EXPERIMENT se hace aqui de forma
    sincrona (sin ceder el control al event loop), de modo que dos peticiones
    concurrentes a /elcm no puedan solapar experimentos sobre la misma TN.

    `dataset` define los formatos de salida de ESTE experimento (body de /elcm).
    """
    record = executions.get(execution_id)
    if not record:
        raise ExecutionNotFoundError(f"Execution '{execution_id}' not found")
    if record.status in {ExecutionState.running_experiment, ExecutionState.collecting}:
        raise ExecutionConflictError(
            "An experiment is already running on this TN; wait for it to finish"
        )
    if record.status != ExecutionState.tn_ready:
        raise ExecutionConflictError(
            f"TN is not ready for experiments (status: {record.status.value})"
        )
    if not record.tn_id:
        raise ExecutionConflictError("TNLCM phase is not ready yet (tn_id missing)")
    if any(run.name == experiment.name for run in record.experiments):
        raise ExecutionConflictError(
            f"Experiment name '{experiment.name}' was already used on this TN; "
            "each experiment must have a unique name"
        )

    record.experiments.append(
        ExperimentRun(
            name=experiment.name,
            started_at=_utc_now_iso(),
            dataset_output=list(dataset.output),
        )
    )
    _update(
        execution_id,
        status=ExecutionState.running_experiment,
        message=f"Experiment '{experiment.name}' accepted",
    )
    _spawn_background_task(
        run_elcm_phase(execution_id, experiment, dataset, ephemeral=ephemeral),
        name=f"elcm:{execution_id}:{experiment.name}",
    )
    return executions[execution_id]


def start_elcm_phase(
    execution_id: str, experiment: ExperimentConfig, dataset: DatasetRequest
) -> ExecutionRecord:
    """Lanza un experimento manual sobre la TN viva (endpoint /elcm).

    ephemeral_tn no aplica en el camino manual: la TN queda viva al terminar.
    `dataset` es la salida de datos pedida para este experimento concreto.
    """
    return _begin_experiment(execution_id, experiment, dataset, ephemeral=False)


async def run_teardown_phase(execution_id: str) -> None:
    """Bloque de borrado de la TN: baja el tunel WireGuard y ejecuta deleted + purged.

    Unica pieza del sistema que destruye una TN operativa; la invocan el
    pipeline efimero (ephemeral_tn=true) y el endpoint DELETE de borrado manual.
    """
    record = executions.get(execution_id)
    if not record or not record.tn_id:
        logger.warning("[%s] Teardown requested but there is no TN to destroy", execution_id)
        return

    tn_id = record.tn_id
    _update(
        execution_id,
        status=ExecutionState.destroying,
        message=f"Destroying TN {tn_id} (deleted + purged)",
    )

    vpn_interface = record.vpn_interface or tn_id
    if vpn_interface:
        try:
            logger.info(f"[{execution_id}] Teardown: deactivating WireGuard tunnel {vpn_interface}")
            wireguard.down_tunnel(vpn_interface, record.vpn_conf_path)
            _update(execution_id, vpn_status="DOWN", vpn_error=None)
        except Exception as vpn_error:
            logger.error(f"[{execution_id}] WireGuard deactivation failed: {vpn_error}")
            _update(execution_id, vpn_status="DOWN_ERROR", vpn_error=str(vpn_error))

    try:
        logger.info(f"[{execution_id}] Teardown: destroying TN {tn_id}")
        await tnlcm.destroy_trial_network(tn_id)
    except Exception as cleanup_error:
        # El timer global queda abierto: el borrado puede reintentarse con
        # otra llamada al endpoint DELETE (se admite desde estado FAILED).
        logger.error(f"[{execution_id}] TN teardown failed: {cleanup_error}")
        _update(
            execution_id,
            status=ExecutionState.failed,
            error=str(cleanup_error),
            message=f"TN {tn_id} teardown failed: {cleanup_error}",
        )
        await _persist_telemetry_report_best_effort(execution_id, "teardown_failed")
        return

    _update(
        execution_id,
        status=ExecutionState.destroyed,
        message=f"TN {tn_id} destroyed and purged",
    )
    telemetry.increment_counter(
        "tn_teardown_total", labels={"service": "orchestrator", "status": "success"}
    )
    await _persist_telemetry_report_best_effort(execution_id, "tn_destroyed")
    logger.info("[%s] TN %s teardown completed", execution_id, tn_id)

    # Cerrar timer global de la ejecucion: el ciclo de vida termina aqui
    final_record = executions.get(execution_id)
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


def start_tn_teardown(execution_id: str) -> ExecutionRecord:
    """Valida y lanza el borrado manual de la TN (endpoint DELETE).

    La transicion a DESTROYING se hace aqui de forma sincrona para que dos
    peticiones de borrado concurrentes no dupliquen los jobs de destroy/purge.
    """
    record = executions.get(execution_id)
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
    if record.status not in {ExecutionState.tn_ready, ExecutionState.failed}:
        raise ExecutionConflictError(
            f"TN cannot be removed in its current state (status: {record.status.value})"
        )

    _update(
        execution_id,
        status=ExecutionState.destroying,
        message=f"TN removal triggered for {record.tn_id}",
    )
    _spawn_background_task(run_teardown_phase(execution_id), name=f"teardown:{execution_id}")
    return executions[execution_id]
