"""Vista de la telemetria orientada al experimentador, no al programador.

``app.utils.telemetry`` es el canal tecnico: emite JSON Lines con vocabulario
interno. Este modulo lo traduce a un resumen legible que responde a las tres preguntas que se hace
quien lanza un experimento: que ha pasado, cuanto ha tardado cada cosa y donde
han quedado los resultados.

No guarda estado propio: se construye bajo demanda cruzando las medidas ya
registradas en el singleton de telemetria (filtradas por ``execution_id``) con
el ``ExecutionRecord``. Por eso puede consultarse en vivo durante el despliegue
y no solo al terminar.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.models import ExecutionRecord, ExecutionState
from app.utils.telemetry import TimingRecord, telemetry

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_RUNNING = "running"
STATUS_PENDING = "pending"
STATUS_SKIPPED = "skipped"

_STATUS_ICONS = {
    STATUS_OK: "✅",
    STATUS_ERROR: "❌",
    STATUS_RUNNING: "⏳",
    STATUS_PENDING: "⬜",
    STATUS_SKIPPED: "➖",
}

_ERROR_TIMING_STATUSES = {"error", "failed"}


@dataclass(frozen=True)
class StepMeta:
    """Como se presenta un par ``(service, operation)`` al experimentador."""

    label: str
    order: int
    technical: bool = False


#: Unico sitio donde vive el vocabulario de cara al usuario. Un par que no este
#: aqui no se pierde: cae al detalle tecnico con la etiqueta ``service/operation``.
STEP_CATALOG: Dict[Tuple[str, str], StepMeta] = {
    # Pasos principales, en el orden en que ocurren.
    ("tnlcm", "create"): StepMeta("Creating the network in TNLCM", 10),
    ("tnlcm", "activate"): StepMeta("Starting up the virtual machines", 20),
    ("wireguard", "tunnel_up"): StepMeta("Opening the VPN tunnel", 30),
    ("orchestrator", "elcm_phase"): StepMeta("Running the experiment", 40),
    ("tnlcm", "destroy"): StepMeta("Releasing the network", 50),
    # Detalle tecnico, plegado por defecto.
    ("orchestrator", "lock_wait"): StepMeta(
        "Waiting for another deployment to finish", 100, technical=True
    ),
    ("tnlcm", "download_report"): StepMeta("Downloading the network report", 110, technical=True),
    ("elcm", "upload"): StepMeta("Uploading test cases", 120, technical=True),
    ("elcm", "run"): StepMeta("Launching the experiment descriptor", 130, technical=True),
    ("elcm", "status"): StepMeta("Checking experiment status", 140, technical=True),
    ("elcm", "collect"): StepMeta("Collecting results", 150, technical=True),
    ("elcm", "results"): StepMeta("Downloading the results bundle", 160, technical=True),
    ("tnlcm", "purged"): StepMeta("Purging the network", 170, technical=True),
    ("wireguard", "tunnel_down"): StepMeta("Closing the VPN tunnel", 180, technical=True),
}

#: Medida que abarca la ejecucion entera, de la peticion al borrado de la TN.
TOTAL_KEY: Tuple[str, str] = ("orchestrator", "execution_total")

#: Agregados de otras medidas: mostrarlos como pasos duplicaria los tiempos.
ROLLUP_KEYS = {
    TOTAL_KEY,
    ("orchestrator", "create"),
    ("orchestrator", "tnlcm_create"),
    ("orchestrator", "tnlcm_phase"),
}

# La telemetria solo registra la medida al detener el timer, asi que un paso en
# curso no tiene todavia ninguna: se deduce del estado de la ejecucion.
_IN_FLIGHT_STEP_BY_STATE: Dict[ExecutionState, Tuple[str, str]] = {
    ExecutionState.validating: ("tnlcm", "create"),
    ExecutionState.deploying: ("tnlcm", "activate"),
    ExecutionState.running_experiment: ("orchestrator", "elcm_phase"),
    ExecutionState.destroying: ("tnlcm", "destroy"),
}

_TERMINAL_STATES = {
    ExecutionState.destroyed,
    ExecutionState.completed,
    ExecutionState.failed,
    ExecutionState.cancelled,
}

_STATE_LABELS: Dict[ExecutionState, str] = {
    ExecutionState.pending: "Queued",
    ExecutionState.validating: "Validating the descriptor",
    ExecutionState.deploying: "Deploying the network",
    ExecutionState.tn_ready: "Network deployed and ready",
    ExecutionState.running_experiment: "Running the experiment",
    ExecutionState.collecting: "Collecting data",
    ExecutionState.destroying: "Releasing the network",
    ExecutionState.destroyed: "Completed",
    ExecutionState.completed: "Completed",
    ExecutionState.failed: "Failed",
    ExecutionState.cancelled: "Cancelled",
}

_EXPERIMENT_STATUS = {"FINISHED": STATUS_OK, "FAILED": STATUS_ERROR}

# (fragmento a buscar en el error, que ha pasado, que puede hacer el usuario).
# El orden importa: gana la primera coincidencia.
_ERROR_HINTS: Tuple[Tuple[str, str, str], ...] = (
    (
        "access token is not loaded",
        "the orchestrator is not logged in to TNLCM",
        "Call POST /login and launch the execution again.",
    ),
    (
        "wireguard_client_config",
        "the network report did not include the VPN configuration",
        "Check in TNLCM that the trial network finished deploying, then try again.",
    ),
    (
        "elcm backend url",
        "the network report did not include the address of the experiment controller",
        "Check the trial network report in TNLCM: the ELCM component may not be deployed.",
    ),
    (
        "not activated",
        "the trial network was not running yet when its report was requested",
        "Wait for the network to finish starting up and launch the execution again.",
    ),
    (
        "timeout",
        "an operation took longer than expected and was cancelled",
        "Check connectivity with TNLCM and ELCM (is the VPN up?) and try again.",
    ),
    (
        "transport error",
        "the orchestrator could not reach one of the remote services",
        "Check connectivity with TNLCM and ELCM (is the VPN up?) and try again.",
    ),
    (
        "logs not found",
        "the experiment finished but its logs could not be retrieved",
        "Check in ELCM that the execution produced output before collecting again.",
    ),
)


def format_duration_human(duration_seconds: Optional[float]) -> Optional[str]:
    """
    Sustituye al ``MM:SS:MMM`` de ``telemetry.format_duration_display``, que se
    lee mal (``04:33:204`` parece 4 horas y media). Aquel se mantiene intacto
    para el informe tecnico.
    """
    if duration_seconds is None:
        return None
    seconds = max(0.0, float(duration_seconds))
    if seconds < 1:
        return "< 1 s"
    if seconds < 60:
        return f"{seconds:.1f} s"
    total = int(round(seconds))
    if total < 3600:
        minutes, rest = divmod(total, 60)
        return f"{minutes} min {rest:02d} s"
    hours, rest = divmod(total, 3600)
    return f"{hours} h {rest // 60:02d} min"


def _humanize_error(error: Optional[str]) -> Optional[str]:
    """Traduce un error tecnico a una frase con sugerencia, si se reconoce."""
    if not error:
        return None
    lowered = error.lower()
    for fragment, what, suggestion in _ERROR_HINTS:
        if fragment in lowered:
            return f"{what[0].upper()}{what[1:]}. Suggestion: {suggestion}"
    # Sin patron conocido se devuelve el mensaje original: es preferible un
    # texto tecnico a ocultarle la causa al experimentador.
    return error


def _elapsed_between(started_at: Optional[str], finished_at: Optional[str]) -> Optional[float]:
    """Segundos entre dos marcas ISO-8601, o ``None`` si falta alguna."""
    if not started_at or not finished_at:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at)
    except (TypeError, ValueError):
        logger.debug("Could not parse experiment timestamps", exc_info=True)
        return None
    return max(0.0, (end - start).total_seconds())


def _group_timings(timings: List[TimingRecord]) -> Dict[Tuple[str, str], List[TimingRecord]]:
    grouped: Dict[Tuple[str, str], List[TimingRecord]] = {}
    for timing in timings:
        grouped.setdefault((timing.service, timing.operation), []).append(timing)
    return grouped


def _build_step(
    label: str,
    timings: List[TimingRecord],
    *,
    missing_status: str = STATUS_PENDING,
) -> Dict[str, Any]:
    """Fila de paso a partir de sus medidas: 0 (aun sin ocurrir), 1 o varias."""
    if not timings:
        return {
            "step": label,
            "status": missing_status,
            "duration": None,
            "duration_seconds": None,
        }

    total = sum(timing.duration_seconds for timing in timings)
    last = timings[-1]
    step: Dict[str, Any] = {
        "step": label,
        "status": STATUS_ERROR if last.status in _ERROR_TIMING_STATUSES else STATUS_OK,
        "duration": format_duration_human(total),
        "duration_seconds": round(total, 3),
    }
    if len(timings) > 1:
        step["attempts"] = len(timings)
    return step


def _in_flight_key(record: ExecutionRecord) -> Optional[Tuple[str, str]]:
    """Paso que se esta ejecutando ahora mismo, segun el estado de la ejecucion."""
    if record.status is ExecutionState.collecting:
        # COLLECTING se usa en dos fases distintas: descargando el report de
        # TNLCM o recolectando el dataset del experimento. Lo desambigua el
        # experimento en vuelo.
        if any(run.status == "RUNNING" for run in record.experiments):
            return ("orchestrator", "elcm_phase")
        return ("tnlcm", "download_report")
    return _IN_FLIGHT_STEP_BY_STATE.get(record.status)


def _missing_status(key: Tuple[str, str], record: ExecutionRecord) -> str:
    if _in_flight_key(record) == key:
        return STATUS_RUNNING
    if record.status in _TERMINAL_STATES:
        return STATUS_SKIPPED
    return STATUS_PENDING


def _experiment_steps(
    record: ExecutionRecord, phase_timings: List[TimingRecord]
) -> List[Dict[str, Any]]:
    """Una fila por experimento, con su nombre, en lugar de un paso generico."""
    steps: List[Dict[str, Any]] = []
    for index, run in enumerate(record.experiments):
        timing = phase_timings[index] if index < len(phase_timings) else None
        seconds = (
            timing.duration_seconds
            if timing is not None
            else _elapsed_between(run.started_at, run.finished_at)
        )
        step: Dict[str, Any] = {
            "step": f'Experiment "{run.name}"',
            "status": _EXPERIMENT_STATUS.get(run.status, STATUS_RUNNING),
            "duration": format_duration_human(seconds),
            "duration_seconds": round(seconds, 3) if seconds is not None else None,
        }
        detail = _humanize_error(run.error)
        if detail:
            step["detail"] = detail
        steps.append(step)
    return steps


def _main_steps(
    record: ExecutionRecord, grouped: Dict[Tuple[str, str], List[TimingRecord]]
) -> List[Dict[str, Any]]:
    main_keys = [key for key, meta in STEP_CATALOG.items() if not meta.technical]
    main_keys.sort(key=lambda key: STEP_CATALOG[key].order)

    steps: List[Dict[str, Any]] = []
    for key in main_keys:
        timings = grouped.get(key, [])
        if key == ("orchestrator", "elcm_phase"):
            experiment_steps = _experiment_steps(record, timings)
            if experiment_steps:
                steps.extend(experiment_steps)
                continue
        steps.append(
            _build_step(
                STEP_CATALOG[key].label,
                timings,
                missing_status=_missing_status(key, record),
            )
        )
    return steps


def _technical_steps(grouped: Dict[Tuple[str, str], List[TimingRecord]]) -> List[Dict[str, Any]]:
    entries: List[Tuple[int, str, List[TimingRecord]]] = []
    for key, timings in grouped.items():
        if key in ROLLUP_KEYS:
            continue
        meta = STEP_CATALOG.get(key)
        if meta is not None and not meta.technical:
            continue
        # Un par no catalogado se muestra igualmente: nunca se pierde una medida.
        order = meta.order if meta is not None else 500
        label = meta.label if meta is not None else f"{key[0]}/{key[1]}"
        entries.append((order, label, timings))

    entries.sort(key=lambda entry: (entry[0], entry[1]))
    return [_build_step(label, timings) for _, label, timings in entries]


def _total_seconds(
    grouped: Dict[Tuple[str, str], List[TimingRecord]], steps: List[Dict[str, Any]]
) -> Optional[float]:
    """Duracion total: el timer global si ya cerro, si no la suma de los pasos."""
    total_timings = grouped.get(TOTAL_KEY, [])
    if total_timings:
        return sum(timing.duration_seconds for timing in total_timings)

    measured = [step["duration_seconds"] for step in steps if step.get("duration_seconds")]
    return sum(measured) if measured else None


def _status_label(record: ExecutionRecord) -> str:
    # TN_READY es tambien el estado final de un experimento fallido: sin esto el
    # resumen diria "ready" en una ejecucion que ha ido mal.
    if record.status is ExecutionState.tn_ready and record.error:
        return "Network ready, but the last experiment failed"
    return _STATE_LABELS.get(record.status, record.status.value)


def _outcome(record: ExecutionRecord) -> str:
    if record.status is ExecutionState.failed or record.error:
        return STATUS_ERROR
    if record.status in _TERMINAL_STATES or record.status is ExecutionState.tn_ready:
        return STATUS_OK
    return STATUS_RUNNING


def _dashboard_url(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle).get("url")
    except Exception:
        logger.debug("Could not read dashboard URL from %s", path, exc_info=True)
        return None


def _result_entries(record: ExecutionRecord) -> Tuple[List[str], List[str]]:
    """Carpetas de resultados y enlaces a dashboards, a partir de los artefactos."""
    directories: List[str] = []
    dashboards: List[str] = []
    for path in record.artifacts:
        normalized = str(path).replace("\\", "/")
        if "/result/" not in normalized:
            continue
        directory = normalized.rsplit("/", 1)[0]
        if directory not in directories:
            directories.append(directory)
        if normalized.endswith("dashboard.json"):
            url = _dashboard_url(path)
            if url and url not in dashboards:
                dashboards.append(url)
    return directories, dashboards


def build_execution_summary(execution_id: str, record: ExecutionRecord) -> Dict[str, Any]:
    """Resumen legible de una ejecucion, apto para fichero, API y UI."""
    grouped = _group_timings(telemetry.timings_for(execution_id))
    steps = _main_steps(record, grouped)
    total = _total_seconds(grouped, steps)
    results, dashboards = _result_entries(record)

    return {
        "execution_id": execution_id,
        "status": _status_label(record),
        "state": record.status.value,
        "outcome": _outcome(record),
        "message": record.message or "",
        "network": record.tn_id,
        "vpn_status": record.vpn_status,
        "total_duration": format_duration_human(total),
        "total_duration_seconds": round(total, 3) if total is not None else None,
        "experiments_total": len(record.experiments),
        "experiments_successful": sum(1 for run in record.experiments if run.status == "FINISHED"),
        "steps": steps,
        "technical_steps": _technical_steps(grouped),
        "results": results,
        "dashboards": dashboards,
        "error": record.error,
        "what_went_wrong": _humanize_error(record.error),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _render_steps_table(steps: List[Dict[str, Any]]) -> List[str]:
    if not steps:
        return ["_No steps recorded yet._", ""]

    rows = ["| Step | Duration | Status |", "|---|---:|:---:|"]
    for step in steps:
        duration = step.get("duration") or "—"
        attempts = step.get("attempts")
        if attempts:
            duration = f"{duration} ({attempts} attempts)"
        icon = _STATUS_ICONS.get(step["status"], "")
        rows.append(f"| {step['step']} | {duration} | {icon} |")
    rows.append("")
    return rows


def render_summary_markdown(summary: Dict[str, Any]) -> str:
    """Renderiza el resumen como Markdown: es el `summary.md` de artifacts/."""
    icon = _STATUS_ICONS.get(summary.get("outcome", ""), "")
    lines: List[str] = [f"# Execution summary — {summary['execution_id']}", ""]

    lines.append(f"- **Status:** {icon} {summary['status']}")
    if summary.get("total_duration"):
        lines.append(f"- **Total duration:** {summary['total_duration']}")
    if summary.get("network"):
        lines.append(f"- **Network:** `{summary['network']}`")
    if summary.get("experiments_total"):
        lines.append(
            f"- **Experiments:** {summary['experiments_successful']} of "
            f"{summary['experiments_total']} successful"
        )
    lines.append("")

    lines.extend(_render_steps_table(summary.get("steps", [])))

    if summary.get("what_went_wrong"):
        lines.append(f"**What went wrong:** {summary['what_went_wrong']}")
        lines.append("")

    for step in summary.get("steps", []):
        if step.get("detail") and step["status"] == STATUS_ERROR:
            lines.append(f"- **{step['step']}:** {step['detail']}")
    if lines[-1].startswith("- **"):
        lines.append("")

    if summary.get("results"):
        lines.append("**Results:**")
        lines.extend(f"- `{directory}`" for directory in summary["results"])
        lines.append("")
    if summary.get("dashboards"):
        lines.append("**Dashboards:**")
        lines.extend(f"- {url}" for url in summary["dashboards"])
        lines.append("")

    technical = summary.get("technical_steps") or []
    if technical:
        lines.append("<details><summary>Technical detail</summary>")
        lines.append("")
        lines.extend(_render_steps_table(technical))
        lines.append("</details>")
        lines.append("")

    lines.append(f"_Generated at {summary['generated_at']}._")
    return "\n".join(lines) + "\n"
