"""Estado de las ejecuciones en curso y su persistencia.

Unica fuente de verdad del `dict` de ejecuciones: toda mutacion pasa por aqui y
persiste a disco en el acto. Las fases nunca tocan el diccionario directamente,
llaman a estas funciones.

Deuda conocida (§9 Fase 1 y 2 del roadmap): es un `dict` en memoria mas un
volcado del JSON completo, y un `threading.Lock` de proceso. No sobrevive a un
reinicio ni funciona con `uvicorn --workers > 1`.
"""

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord
from app.observability.telemetry import telemetry
from app.services.errors import (
    ExecutionNotFoundError,
    PhaseStillRunningError,
    TnlcmDeploymentInProgressError,
)

logger = logging.getLogger(__name__)

# Estado en memoria (MVP)
executions: dict[str, ExecutionRecord] = {}

# Un unico despliegue TNLCM a la vez: activar dos TN en paralelo se pisan en el
# facility. El guard es de proceso, no de despliegue (ver deuda arriba).
_tnlcm_deploy_guard = threading.Lock()
_tnlcm_deploy_in_progress: str | None = None

# Estados del record desde los que se intenta reconciliar con TNLCM antes de
# rechazar una peticion: el record y TNLCM pueden divergir (un teardown que no
# llego a purgar deja el record en DESTROYED con la TN todavia en pie).
RECONCILABLE_STATES = frozenset({ExecutionState.failed, ExecutionState.destroyed})

EXECUTIONS_FILE = Path(settings.executions_file)
EXECUTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_to_disk() -> None:
    """Guarda el estado de las ejecuciones a disco en JSON."""
    try:
        data = {execution_id: record.model_dump() for execution_id, record in executions.items()}
        with open(EXECUTIONS_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as exc:
        logger.warning(f"Could not save executions to disk: {exc}")


def load_from_disk() -> None:
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


def get_execution(execution_id: str) -> ExecutionRecord | None:
    return executions.get(execution_id)


def signal_phase(execution_id: str, signal: str) -> None:
    """Marca una fase como terminada, haya ido bien o mal.

    `Event.set()` es idempotente, asi que puede llamarse desde varios puntos de
    salida de la misma fase sin efectos secundarios.
    """
    record = executions.get(execution_id)
    if record is None:
        return
    event = getattr(record, signal, None)
    if event is not None:
        event.set()


def clear_phase_signal(execution_id: str, signal: str) -> None:
    """Rearma una señal antes de relanzar la fase (p. ej. otro experimento)."""
    record = executions.get(execution_id)
    if record is None:
        return
    event = getattr(record, signal, None)
    if event is not None:
        event.clear()


async def wait_for_phase(execution_id: str, signal: str, timeout: float) -> ExecutionRecord:
    """Espera a que una fase alcance su estado terminal.

    La señal solo indica que la fase termino; el resultado se lee despues de
    `record.status` y `record.error`. Al agotarse el tope se lanza
    `PhaseStillRunningError` y la fase continua en segundo plano.
    """
    record = executions.get(execution_id)
    if record is None:
        raise ExecutionNotFoundError(f"Execution '{execution_id}' not found")

    event: asyncio.Event = getattr(record, signal)
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise PhaseStillRunningError(
            f"Execution '{execution_id}' is still running after {timeout:.0f}s. "
            f"It keeps going in the background: check GET /executions/{execution_id}"
        ) from exc
    return executions[execution_id]


def update(execution_id: str, **kwargs) -> None:
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

    save_to_disk()  # Guarda cambios inmediatamente


def flush_artifacts(execution_id: str, new_paths: list[str]) -> None:
    """Vuelca artefactos al record en cuanto existen, sin esperar al final.

    Permite que /summary refleje cada salida del dataset segun se genera y que
    un corte por tope no deje ficheros en disco invisibles para la API.
    """
    record = executions.get(execution_id)
    if record is None or not new_paths:
        return
    update(execution_id, artifacts=list(dict.fromkeys([*record.artifacts, *new_paths])))


def set_experiment_run_fields(execution_id: str, name: str, **kwargs) -> None:
    """Actualiza el ExperimentRun mas reciente con ese nombre y persiste."""
    record = executions.get(execution_id)
    if not record:
        return
    for run in reversed(record.experiments):
        if run.name == name:
            for key, value in kwargs.items():
                setattr(run, key, value)
            break
    save_to_disk()


def acquire_tnlcm_deploy_slot(execution_id: str) -> None:
    global _tnlcm_deploy_in_progress
    with _tnlcm_deploy_guard:
        if _tnlcm_deploy_in_progress is not None:
            raise TnlcmDeploymentInProgressError(
                "Ya existe un despliegue/activacion TNLCM en curso. "
                "Espere a que termine antes de lanzar otra peticion."
            )
        _tnlcm_deploy_in_progress = execution_id


def release_tnlcm_deploy_slot(execution_id: str) -> None:
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
