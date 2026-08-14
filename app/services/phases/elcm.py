"""Fase ELCM: renderiza y sube los TestCases, lanza el experimento y recolecta.

Es la fase mas larga del ciclo: subir artefactos, arrancar, hacer polling hasta
que ELCM declara el final y, solo entonces, recoger cada formato de entrega
pedido en `dataset.output`.
"""

import asyncio
import logging
from typing import Any

from app.adapters import elcm, tnlcm
from app.adapters.elcm import TnLogsNotFoundError
from app.domain.descriptor import DatasetRequest, ExperimentConfig
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord, ExperimentRun
from app.observability.telemetry import telemetry
from app.rendering.elcm import dataset as elcm_dataset
from app.services import background, reporting, state
from app.services.errors import ExecutionConflictError, ExecutionNotFoundError
from app.services.phases import results
from app.services.phases.teardown import run_teardown_phase
from app.storage import artifacts

logger = logging.getLogger(__name__)

# Tiempos de la fase.
ELCM_POLL_INTERVAL_SECONDS = 10
ELCM_EXECUTION_TIMEOUT_SECONDS = 3600

# Tope de la recoleccion completa del dataset. Cada llamada HTTP lleva su propio
# timeout, pero `raw` vuelca un measurement por consulta y su numero no se conoce
# de antemano: este tope es el que permite declarar cuanto puede tardar el endpoint.
DATASET_MAX_SECONDS = 600

# Formatos de dataset.output realmente implementados en el runtime. El esquema
# acepta mas, pero se activan de forma incremental; pedir uno no implementado
# aborta la ejecucion (fail-fast). Al implementar uno nuevo, se anade aqui.
IMPLEMENTED_DATASET_OUTPUTS: set[str] = {"logs", "csv", "dashboard", "raw", "files"}


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


def _dataset_data_values(
    kind: str,
    execution_id: str,
    dataset_variables: dict[str, Any],
    user_testcase_files: list[str],
) -> dict[str, Any] | None:
    """Resolver los `data.values` de ytt para el TestCase de dataset `kind`.

    Precedencia de cada variable: valor del body -> valor derivado del despliegue
    -> default declarado en el overlay (se consigue no emitiendo la clave).
    Derivar en vez de hardcodear es lo que permite que el TestCase apunte a la
    monitorización real de la TN y no a la IP de laboratorio del overlay.
    """
    values: dict[str, Any] = {}

    # measurement: el del body; si no, el del TestCase de captura del experimento.
    capture = elcm.extract_capture_metrics(user_testcase_files)
    measurement = dataset_variables.get("measurement")
    if measurement is None and capture is not None:
        measurement = capture[0]

    if kind == "dashboard":
        # El dashboard se genera con un panel por métrica, y las métricas solo
        # pueden salir del TestCase de captura: sin él no hay nada que pintar.
        if capture is None:
            raise ValueError(
                "dataset.output 'dashboard' requiere un TestCase de captura "
                "(*_capture* con Run.PrometheusToInflux) en testcase_paths"
            )
        values["metrics"] = capture[1]
        if dataset_variables.get("panel_interval") is not None:
            values["interval"] = dataset_variables["panel_interval"]

    if kind == "csv":
        # La IP de InfluxDB es la de monitorización de ESTA TN, salvo que el body
        # diga otra cosa. Si el report aún no está disponible se deja el default
        # del overlay en vez de abortar: el TestCase se genera igualmente.
        influx_host = dataset_variables.get("influx_host")
        if influx_host is None:
            try:
                influx_host = (artifacts.load_monitoring_info(execution_id) or {}).get("ip")
            except (OSError, ValueError):
                # Sin report todavía (FileNotFoundError) o report ilegible
                # (JSONDecodeError): se cae al default del overlay.
                logger.debug(
                    "[%s] Monitoring info unavailable for dataset csv host",
                    execution_id,
                    exc_info=True,
                )
                influx_host = None
        influx: dict[str, Any] = {}
        if influx_host:
            influx["host"] = influx_host
        if dataset_variables.get("influx_port") is not None:
            influx["port"] = dataset_variables["influx_port"]
        if influx:
            values["influx"] = influx

    if kind in ("csv", "raw") and dataset_variables.get("influx_bucket") is not None:
        values["bucket"] = dataset_variables["influx_bucket"]

    if measurement is not None:
        values["measurement"] = measurement

    return {"dataset": values} if values else None


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
    record = state.executions[execution_id]
    tn_id = record.tn_id
    elcm_base_url = record.elcm_base_url

    # Formatos de salida de ESTE experimento (por defecto, los de la ejecución).
    dataset_outputs = list(dataset.output) if dataset is not None else list(record.dataset_output)
    # Variables globales del bloque dataset; mismo criterio de herencia.
    dataset_variables = (
        dict(dataset.variables()) if dataset is not None else dict(record.dataset_variables)
    )

    if not tn_id:
        state.update(
            execution_id, status=ExecutionState.failed, message="tn_id missing for ELCM phase"
        )
        state.signal_phase(execution_id, "_experiment_finished")
        return
    if not elcm_base_url:
        state.update(
            execution_id,
            status=ExecutionState.failed,
            message="elcm_base_url missing for ELCM phase",
        )
        state.signal_phase(execution_id, "_experiment_finished")
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
        state.update(
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
        state.set_experiment_run_fields(
            execution_id,
            experiment.name,
            dataset_output=list(dataset_outputs),
            dataset_variables=dict(dataset_variables),
        )

        testcase_list = _get_testcases(experiment)
        if not testcase_list:
            raise ValueError("At least one testcase is required")

        # Los TestCases del body se resuelven contra la biblioteca
        # (templates/ELCM/TestCase/) y se suben TAL CUAL: no se re-renderizan
        # (eso corrompía el entrecomillado y la indentación). Fail-fast si
        # alguno no existe.
        generated_testcase_paths: list[str] = [
            str(elcm.resolve_testcase_file(testcase_ref)) for testcase_ref in testcase_list
        ]
        # Ficheros del usuario antes de inyectar los TC de dataset: de aqui se lee el
        # TestCase de captura (*_capture*) para el dashboard.
        user_testcase_files = list(generated_testcase_paths)

        # Los UEs son ficheros de variables globales (Run.Publish): se resuelven igual
        # que los TestCases pero se suben a otra carpeta de ELCM (file_type="ues").
        ue_files: list[str] = [
            str(elcm.resolve_ue_file(ue_ref)) for ue_ref in experiment.ues_paths if ue_ref
        ]

        # Inyección de TestCases de dataset (csv/dashboard): se generan con ytt y
        # se añaden a la lista de TestCases (upload + descriptor) para que ELCM
        # los ejecute y produzca el CSV / cree el dashboard. Se guardan como
        # <Name>.yml. (raw NO inyecta TestCase: consultará InfluxDB directamente.)
        for kind in ("csv", "dashboard"):
            if kind in dataset_outputs and kind in elcm_dataset.ELCM_DATASET_TEMPLATES:
                data_values = _dataset_data_values(
                    kind,
                    execution_id,
                    dataset_variables,
                    user_testcase_files,
                )
                dataset_tc_path = await elcm_dataset.generate_elcm_dataset_testcase(
                    kind, execution_id, data_values=data_values
                )
                generated_testcase_paths.append(str(dataset_tc_path))
                logger.info("[%s] Injected %s dataset testcase into experiment", execution_id, kind)

        state.update(execution_id, message="Generating Experiment Descriptor")
        experiment_descriptor_path = await elcm.generate_experiment_descriptor(
            experiment,
            generated_testcase_paths,
            execution_id,
        )

        # Los UEs se suben primero: publican las variables globales que los
        # TestCases consumen con @[...].
        if ue_files:
            state.update(execution_id, message="Uploading UEs")
            await elcm.upload_test_cases(
                ue_files,
                elcm_base_url=elcm_base_url,
                execution_id=execution_id,
                file_type="ues",
            )

        state.update(execution_id, message="Uploading TestCases")
        await elcm.upload_test_cases(
            generated_testcase_paths, elcm_base_url=elcm_base_url, execution_id=execution_id
        )

        state.update(execution_id, message="Launching experiment descriptor")
        elcm_execution_id = await elcm.run_experiment(
            experiment,
            elcm_base_url=elcm_base_url,
            execution_id=execution_id,
            exp_descriptor_path=experiment_descriptor_path,
        )
        experiment_ids = list(dict.fromkeys([*record.experiment_ids, elcm_execution_id]))
        state.update(execution_id, elcm_execution_id=elcm_execution_id)
        state.set_experiment_run_fields(
            execution_id, experiment.name, elcm_execution_id=elcm_execution_id
        )

        # Poll until terminal status using configurable ELCM timing.
        exp_done = False
        timeout_seconds = ELCM_EXECUTION_TIMEOUT_SECONDS
        poll_interval_seconds = ELCM_POLL_INTERVAL_SECONDS
        elapsed = 0

        # Estados terminales de ELCM (CoarseStatus: Init, PreRun, Run, PostRun,
        # Finished, Cancelled, Errored). 'ERR' lo devuelve su scheduler cuando
        # ni siquiera puede leer la lapida de la ejecucion, y es tan terminal
        # como los demas: sin tratarlos, el bucle giraria hasta agotar
        # ELCM_EXECUTION_TIMEOUT_SECONDS con el cliente esperando.
        success_statuses = {"FINISHED", "COMPLETED", "DONE"}
        failure_statuses = {"CANCELLED", "CANCELED", "ERRORED", "FAILED", "ERR"}

        while elapsed < timeout_seconds:
            exp_status = await elcm.get_experiment_status(
                elcm_execution_id, elcm_base_url=elcm_base_url, execution_id=execution_id
            )
            logger.info(f"ELCM execution {elcm_execution_id} status: {exp_status}")
            normalized = exp_status.strip().upper()

            # Check if execution is finished
            if "FINISHED" in normalized or normalized in success_statuses:
                exp_done = True
                break

            # Check for errors, cancellations and unreadable executions
            if normalized in failure_statuses or "ERROR" in normalized or "FAILED" in normalized:
                raise RuntimeError(
                    f"ELCM execution {elcm_execution_id} did not complete "
                    f"(status: {exp_status})"
                )

            await asyncio.sleep(poll_interval_seconds)
            elapsed += poll_interval_seconds

        if not exp_done:
            raise TimeoutError(f"Timeout waiting for ELCM execution {elcm_execution_id} to finish")

        # --- Recolección de outputs del dataset (según dataset.output) ---
        state.update(execution_id, status=ExecutionState.collecting, message="Collecting dataset")
        collected_artifacts: list[str] = []
        completed_outputs: list[str] = []

        def _record_output(name: str, paths: list[str]) -> None:
            """Registra una salida y la vuelca al record en cuanto existe.

            El volcado incremental hace que /summary refleje cada salida segun
            se genera, y evita que un corte por tope deje ficheros en disco
            invisibles para la API.
            """
            collected_artifacts.extend(paths)
            completed_outputs.append(name)
            state.flush_artifacts(execution_id, paths)

        async def _collect_dataset_outputs() -> None:
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

                logs_payload = {
                    "output": "logs",
                    "experiment_ids": experiment_ids,
                    "testcases": testcase_list,
                    "logs": execution_logs,
                }
                _record_output(
                    "logs",
                    await artifacts.build_artifacts(
                        execution_id,
                        tn_id,
                        elcm_execution_id,
                        logs_payload,
                        experiment_name=experiment.name,
                    ),
                )

            # csv: descargar el ZIP de resultados de ELCM y extraer el/los CSV.
            if "csv" in dataset_outputs:
                _record_output(
                    "csv",
                    await results.collect_csv(
                        execution_id, elcm_execution_id, elcm_base_url, experiment.name
                    ),
                )

            # dashboard: construir y guardar la URL del dashboard Grafana.
            if "dashboard" in dataset_outputs:
                _record_output(
                    "dashboard",
                    await results.collect_dashboard(
                        execution_id, elcm_execution_id, experiment.name
                    ),
                )

            # raw: consultar InfluxDB directamente y volcar el CSV crudo por measurement.
            if "raw" in dataset_outputs:
                _record_output(
                    "raw",
                    await results.collect_raw(
                        execution_id, elcm_execution_id, experiment.name, dataset_variables
                    ),
                )

            # files: descargar el ZIP de resultados y extraer TODOS los ficheros (sin inyectar).
            if "files" in dataset_outputs:
                _record_output(
                    "files",
                    await results.collect_files(
                        execution_id, elcm_execution_id, elcm_base_url, experiment.name
                    ),
                )

        # Tope global de la recolección: cada llamada HTTP ya lleva el suyo,
        # pero `raw` vuelca un measurement por consulta y su número no se
        # conoce de antemano. Agotarlo no es un fallo del experimento: se
        # conserva lo recolectado y se deja constancia de lo que falta.
        dataset_partial: str | None = None
        try:
            await asyncio.wait_for(_collect_dataset_outputs(), timeout=DATASET_MAX_SECONDS)
        except asyncio.TimeoutError:
            missing = [fmt for fmt in dataset_outputs if fmt not in completed_outputs]
            dataset_partial = (
                f"Partial dataset after {DATASET_MAX_SECONDS}s: "
                f"collected {', '.join(completed_outputs) or 'nothing'}; "
                f"missing {', '.join(missing)}"
            )
            logger.warning(f"[{execution_id}] {dataset_partial}")
            telemetry.increment_counter(
                "errors_total",
                labels={
                    "service": "orchestrator",
                    "operation": "elcm_phase",
                    "error_type": "dataset_timeout",
                },
            )

        state.update(
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
        state.set_experiment_run_fields(
            execution_id,
            experiment.name,
            status="FINISHED",
            finished_at=state.utc_now_iso(),
            error=dataset_partial,
        )
        # Dataset parcial: el experimento SI termino, solo falto parte de la
        # recoleccion. Se usa el idioma ya existente en el proyecto (estado
        # terminal + campo `error` relleno), como en la VPN MANUAL_REQUIRED.
        state.update(
            execution_id,
            status=ExecutionState.tn_ready,
            artifacts=merged_artifacts,
            error=dataset_partial,
            message=(
                f"Experiment '{experiment.name}' finished with a partial dataset. TN still alive."
                if dataset_partial
                else f"Experiment '{experiment.name}' finished. TN still alive."
            ),
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
        state.set_experiment_run_fields(
            execution_id,
            experiment.name,
            status="FAILED",
            error=str(exc),
            finished_at=state.utc_now_iso(),
        )
        # La TN sigue viva: se vuelve a TN_READY para permitir reintentar otro
        # experimento o lanzar el borrado manual. El error queda registrado.
        state.update(
            execution_id,
            status=ExecutionState.tn_ready,
            error=str(exc),
            message=f"Experiment '{experiment.name}' failed: {exc}. TN still alive.",
        )

    finally:
        final_record = state.executions.get(execution_id)
        final_stage = (
            "elcm_completed"
            if final_record and final_record.status == ExecutionState.tn_ready
            else "elcm_finalized"
        )
        await reporting.persist_telemetry_report_best_effort(execution_id, final_stage)
        logger.info("[%s] ELCM phase finalization completed", execution_id)

        # Antes de encadenar el teardown efimero: quien espera en /elcm quiere
        # su dataset, no la destruccion de la TN.
        state.signal_phase(execution_id, "_experiment_finished")

        # TN de un solo uso: encadenar el bloque de borrado tras el experimento
        # automatico, tanto si termino bien como si fallo.
        if ephemeral:
            logger.info("[%s] ephemeral_tn=true: chaining TN teardown", execution_id)
            await run_teardown_phase(execution_id)


def _not_ready_detail(record: ExecutionRecord) -> str:
    """Mensaje del 409 cuando la TN no admite experimentos.

    Desde DESTROYED/FAILED se ha intentado ya reconciliar con TNLCM, asi que
    llegar aqui significa que la TN realmente no esta: el mensaje dice cual es
    el siguiente paso en vez de dejar solo el estado interno.
    """
    if record.status in state.RECONCILABLE_STATES:
        return (
            f"TN is not ready for experiments (status: {record.status.value}) and TNLCM no "
            f"longer reports it as alive; deploy it again with POST /executions"
        )
    return f"TN is not ready for experiments (status: {record.status.value})"


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
    record = state.executions.get(execution_id)
    if not record:
        raise ExecutionNotFoundError(f"Execution '{execution_id}' not found")
    if record.status in {ExecutionState.running_experiment, ExecutionState.collecting}:
        raise ExecutionConflictError(
            "An experiment is already running on this TN; wait for it to finish"
        )
    if record.status != ExecutionState.tn_ready:
        raise ExecutionConflictError(_not_ready_detail(record))
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
            started_at=state.utc_now_iso(),
            dataset_output=list(dataset.output),
            dataset_variables=dict(dataset.variables()),
        )
    )
    # `error` se limpia al aceptar: es el error de la ejecucion, y arrastrar el
    # del experimento anterior hacia que /summary dijera que algo fue mal aunque
    # el nuevo experimento terminara bien. El historial por experimento se
    # conserva en `record.experiments[].error`.
    state.update(
        execution_id,
        status=ExecutionState.running_experiment,
        error=None,
        message=f"Experiment '{experiment.name}' accepted",
    )
    # Rearmar: sobre una misma TN se lanzan varios experimentos y la señal
    # sigue activada por el anterior.
    state.clear_phase_signal(execution_id, "_experiment_finished")
    background.spawn_background_task(
        run_elcm_phase(execution_id, experiment, dataset, ephemeral=ephemeral),
        name=f"elcm:{execution_id}:{experiment.name}",
    )
    return state.executions[execution_id]
