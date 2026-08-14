"""Lanzamiento de experimentos ELCM sobre una TN ya viva."""

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import verify_api_key
from app.api.phases import EXPERIMENT_PHASE_RESPONSES, await_phase, to_execution_response
from app.api.schemas.requests import ElcmExperimentRequest
from app.api.schemas.responses import ExecutionResponse
from app.api.validation import validate_elcm_or_raise
from app.services import orchestrator
from app.services.errors import ExecutionConflictError, ExecutionNotFoundError

router = APIRouter(tags=["executions"], dependencies=[Depends(verify_api_key)])


@router.post(
    "/executions/{execution_id}/elcm",
    response_model=ExecutionResponse,
    status_code=200,
    responses=EXPERIMENT_PHASE_RESPONSES,
)
async def post_execution_elcm(
    execution_id: str,
    request: ElcmExperimentRequest,
    response: Response,
    wait: bool = Query(
        True,
        description=(
            "Si es true (por defecto) la respuesta no llega hasta que el dataset "
            "esta recolectado. Con false se devuelve 202 al instante."
        ),
    ),
):
    """Lanza un experimento ELCM sobre la TN viva de la ejecucion.

    Con `wait=true` la respuesta llega cuando el dataset ya esta en disco, no
    solo cuando el experimento ha parado. El codigo HTTP dice como fue: 200
    dataset completo, 207 el experimento termino pero la recoleccion quedo a
    medias (el campo `error` dice que formatos faltaron), 502 el experimento
    fallo. Tope de espera: 70 min; al agotarse se responde 504 y el experimento
    continua en segundo plano.

    Puede llamarse tantas veces como experimentos se quieran ejecutar (uno a
    la vez). Cada experimento debe tener un nombre unico dentro de la TN.

    El body admite `dataset.output` propio: cada experimento puede pedir una
    salida de datos distinta (logs/csv/dashboard/raw), que se recolecta en
    artifacts/<execution_id>/result/<experimento>/.

    Si la ejecucion quedo en DESTROYED o FAILED pero TNLCM sigue teniendo la TN
    viva, se recupera automaticamente (reabriendo el tunel WireGuard) para poder
    corregir y relanzar el experimento sin volver a desplegar por /executions.

    Respuestas: 200 experimento terminado; 202 aceptado (wait=false);
    400 la parte ELCM del body no es valida; 404 la ejecucion no existe; 409 hay
    un experimento en curso, la TN ya no existe o el nombre esta repetido; 504
    sigue en curso al agotarse el tope.
    """
    # Antes de start_elcm_phase: un body invalido no debe llegar a anotar un
    # experimento FAILED ni a mover el estado de la ejecucion.
    validate_elcm_or_raise(request.experiment, request.dataset)

    try:
        record = await orchestrator.start_elcm_phase(
            execution_id, request.experiment, request.dataset
        )
    except ExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ExecutionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    if not wait:
        response.status_code = 202
        return to_execution_response(record)
    return await await_phase(
        execution_id,
        "_experiment_finished",
        orchestrator.ELCM_PHASE_MAX_WAIT_SECONDS,
        response,
        experiment_name=request.experiment.name,
    )
