"""Traduccion del desenlace de una fase del ciclo de vida a respuesta HTTP.

La regla que sostiene todo el contrato de la API: **el codigo HTTP es la
respuesta**. 200 solo si la fase hizo todo lo que el endpoint promete, 207 si
termino incompleta, 502 si fallo y 504 si sigue en curso al agotarse el tope. El
cliente no tiene que leer el cuerpo para saber como fue.
"""

from typing import Any

from fastapi import HTTPException, Response

from app.api.schemas.responses import ExecutionResponse
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord
from app.services import orchestrator
from app.services.errors import ExecutionNotFoundError, PhaseStillRunningError

# Desenlaces posibles de una fase, documentados en OpenAPI.
PHASE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "La fase completo todo lo que el endpoint promete"},
    207: {"description": "La fase termino, pero incompleta (ver vpn_status / error)"},
    502: {"description": "La fase fallo en un servicio de abajo (TNLCM/Jenkins/ELCM)"},
    504: {"description": "Sigue en curso al agotarse el tope; consultar GET /executions/{id}"},
}

# Los endpoints que aceptan un experimento validan su parte ELCM antes de
# aceptar la peticion, asi que suman un desenlace propio al resto de la fase.
EXPERIMENT_PHASE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **PHASE_RESPONSES,
    400: {"description": "La parte ELCM del body no es valida (ver invalid_experiment)"},
}


def to_execution_response(record: ExecutionRecord) -> ExecutionResponse:
    return ExecutionResponse(
        execution_id=record.execution_id,
        status=record.status,
        message=record.message,
        tn_id=record.tn_id,
        vpn_status=record.vpn_status,
        error=record.error,
    )


def phase_http_status(
    record: ExecutionRecord, signal: str, experiment_name: str | None = None
) -> int:
    """Traduce el desenlace de una fase al codigo HTTP que le corresponde."""
    if record.status is ExecutionState.failed:
        return 502

    if signal == "_vpn_ready":
        # TN desplegada pero sin tunel automatico: no se puede llamar a /elcm
        # todavia, asi que no es un 200 limpio.
        return 207 if record.vpn_status == "MANUAL_REQUIRED" else 200

    if signal == "_experiment_finished":
        run = next(
            (item for item in reversed(record.experiments) if item.name == experiment_name),
            None,
        )
        if run is None:
            return 200
        if run.status == "FAILED":
            return 502
        # Experimento terminado con `error` relleno = dataset parcial.
        return 207 if run.error else 200

    if signal == "_tn_purged":
        return 200 if record.status is ExecutionState.destroyed else 502

    return 200


async def await_phase(
    execution_id: str,
    signal: str,
    timeout: float,
    response: Response,
    *,
    experiment_name: str | None = None,
) -> ExecutionResponse:
    """Bloquea hasta que la fase termine y responde con el codigo del desenlace."""
    try:
        record = await orchestrator.wait_for_phase(execution_id, signal, timeout)
    except ExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PhaseStillRunningError as exc:
        raise HTTPException(status_code=504, detail=str(exc))

    response.status_code = phase_http_status(record, signal, experiment_name)
    return to_execution_response(record)
