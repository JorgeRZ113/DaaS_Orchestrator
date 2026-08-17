"""Ciclo de vida de una ejecucion: crear la TN, consultarla y borrarla."""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse

from app.api.deps import verify_api_key
from app.api.phases import (
    EXPERIMENT_PHASE_RESPONSES,
    PHASE_RESPONSES,
    await_phase,
    to_execution_response,
)
from app.api.schemas.responses import (
    ExecutionDetailResponse,
    ExecutionResponse,
    ExecutionSummary,
)
from app.api.validation import (
    reject_empty_strings_or_raise,
    validate_components_or_raise,
    validate_elcm_or_raise,
)
from app.domain.descriptor import DatasetDescriptor
from app.observability.execution_summary import build_execution_summary, render_summary_markdown
from app.observability.telemetry import format_duration_display, telemetry
from app.rendering.tnlcm.overlay import InvalidDatasetDescriptorError
from app.services import orchestrator
from app.services.errors import (
    ExecutionConflictError,
    ExecutionNotFoundError,
    TnlcmDeploymentInProgressError,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["executions"], dependencies=[Depends(verify_api_key)])


@router.post(
    "/executions",
    response_model=ExecutionResponse,
    status_code=200,
    responses=EXPERIMENT_PHASE_RESPONSES,
)
async def post_execution(
    descriptor: DatasetDescriptor,
    response: Response,
    wait: bool = Query(
        True,
        description=(
            "Si es true (por defecto) la respuesta no llega hasta que la VPN queda "
            "resuelta. Con false se devuelve 202 al instante y se consulta el estado "
            "con GET /executions/{execution_id}."
        ),
    ),
):
    """Inicia una ejecución completa: TNLCM y opcionalmente ELCM (auto-start).

    Con `wait=true` la respuesta llega cuando la VPN esta resuelta, que es el
    punto a partir del cual se puede llamar a /elcm. El codigo HTTP dice como
    fue: 200 tunel arriba, 207 TN desplegada pero el tunel hay que montarlo a
    mano (vpn_status=MANUAL_REQUIRED), 502 fallo el despliegue. Tope de espera:
    40 min; al agotarse se responde 504 y el despliegue continua en segundo
    plano.

    Si descriptor.auto_start_elcm=true (por defecto), ELCM se inicia automáticamente
    al completar TNLCM. Establecer auto_start_elcm=false para control manual del flujo.

    dataset.output acepta un nombre o una lista combinable de: logs, csv, dashboard,
    raw. Las respuestas del dataset se guardan en artifacts/<execution_id>/result/.
    """
    reject_empty_strings_or_raise(descriptor)

    try:
        validate_components_or_raise(descriptor.infrastructure)
    except HTTPException:
        raise
    except InvalidDatasetDescriptorError as exc:
        raise HTTPException(status_code=400, detail={"invalid_fields": exc.invalid_fields})

    # La parte ELCM se comprueba aqui y no en la fase: con auto-start el fallo
    # llegaria despues de desplegar la TN, dejando una TN viva y un experimento
    # FAILED por un nombre mal escrito. Solo se valida si el experimento se va a
    # ejecutar (sin auto-start el pipeline lo ignora).
    if descriptor.auto_start_elcm and descriptor.experiment is not None:
        validate_elcm_or_raise(descriptor.experiment, descriptor.dataset)

    execution_id = descriptor.infrastructure.name.strip()
    telemetry.increment_counter(
        "requests_total", labels={"service": "orchestrator", "operation": "create"}
    )
    telemetry.log_event(
        "info",
        "request.received",
        service="orchestrator",
        operation="create",
        execution_id=execution_id,
    )
    timer = telemetry.start_timer("orchestrator", "create", execution_id)
    timer.start()
    request_status = "success"
    logger.info(
        f"Nueva ejecucion solicitada (auto_elcm={descriptor.auto_start_elcm}): "
        f"{descriptor.infrastructure.name}"
    )
    try:
        record = await orchestrator.create_tnlcm_execution(descriptor)
        if not wait:
            response.status_code = 202
            return to_execution_response(record)
        return await await_phase(
            record.execution_id,
            "_vpn_ready",
            orchestrator.TNLCM_PHASE_MAX_WAIT_SECONDS,
            response,
        )
    except TnlcmDeploymentInProgressError as exc:
        request_status = "error"
        telemetry.increment_counter(
            "errors_total",
            labels={
                "service": "orchestrator",
                "operation": "create",
                "error_type": "tnlcm_deploy_in_progress",
            },
        )
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        request_status = "error"
        raise
    finally:
        try:
            duration = timer.stop(status=request_status)
            payload = {
                "service": "orchestrator",
                "operation": "create",
                "execution_id": execution_id,
            }
            if duration >= 1.0:
                payload["duration_display"] = format_duration_display(duration)
            telemetry.log_event("info", "request.completed", **payload)
        except Exception:
            pass


@router.delete(
    "/executions/{execution_id}/tn",
    response_model=ExecutionResponse,
    status_code=200,
    responses=PHASE_RESPONSES,
)
async def delete_execution_tn(
    execution_id: str,
    response: Response,
    wait: bool = Query(
        True,
        description=(
            "Si es true (por defecto) la respuesta no llega hasta que la TN esta "
            "purgada. Con false se devuelve 202 al instante."
        ),
    ),
):
    """Dispara el bloque de borrado de la TN (deleted + purged) bajo demanda.

    Con `wait=true` la respuesta llega cuando la TN esta purgada: 200 si se
    purgo, 502 si el destroy fallo (la TN sigue existiendo y el borrado se
    puede reintentar). Tope de espera: 50 min; al agotarse se responde 504 y el
    borrado continua en segundo plano.

    La respuesta indica en `tn_id` que Trial Network se esta borrando.

    Respuestas: 200 TN purgada; 202 borrado lanzado (wait=false); 404 la
    ejecucion no existe o no tiene TN; 409 hay un experimento en curso o el
    borrado ya se lanzo/completo; 504 sigue en curso al agotarse el tope.
    """
    try:
        record = orchestrator.start_tn_teardown(execution_id)
    except ExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ExecutionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    if not wait:
        response.status_code = 202
        return to_execution_response(record)
    return await await_phase(
        execution_id, "_tn_purged", orchestrator.TEARDOWN_MAX_WAIT_SECONDS, response
    )


@router.get("/executions/{execution_id}", response_model=ExecutionResponse)
async def get_execution_status(execution_id: str):
    """Devuelve el estado resumido de una ejecucion."""
    record = orchestrator.get_execution(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Ejecucion no encontrada")
    return to_execution_response(record)


@router.get("/executions/{execution_id}/detail", response_model=ExecutionDetailResponse)
async def get_execution_detail(execution_id: str):
    """Devuelve el registro completo (incluye artifacts y error).

    A los campos del record se suma `tn_state`: el estado que TNLCM reporta en
    ese momento para la TN (`created`, `activated`, `destroyed`...), consultado
    en vivo contra TNLCM. Sirve para ver el detalle real de la TN cuando el
    `status` propio se queda corto (p. ej. la TN se destruyo por fuera). Es
    best-effort: llega a null si la ejecucion aun no tiene TN o si TNLCM no
    responde, sin que eso afecte al resto de la respuesta.
    """
    record = orchestrator.get_execution(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Ejecucion no encontrada")
    tn_state = await orchestrator.probe_tn_state(record)
    return ExecutionDetailResponse(**record.model_dump(), tn_state=tn_state)


@router.get(
    "/executions/{execution_id}/summary",
    response_model=ExecutionSummary,
    # Sin los campos vacios: la respuesta queda igual que el `summary.json` de
    # artifacts/ y se lee sin ruido (`attempts`/`detail` solo cuando aplican).
    response_model_exclude_none=True,
)
async def get_execution_summary(
    execution_id: str,
    output_format: Literal["json", "markdown"] = Query("json", alias="format"),
):
    """Devuelve el resumen legible de una ejecucion, pensado para experimentadores.

    Muestra que paso en cada fase, cuanto tardo y donde han quedado los
    resultados, sin vocabulario interno. Se construye en vivo, asi que puede
    consultarse mientras la ejecucion sigue en curso.

    Con `?format=markdown` devuelve el mismo contenido como texto (el mismo
    `summary.md` que se guarda en `artifacts/<execution_id>/`).
    """
    record = orchestrator.get_execution(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Ejecucion no encontrada")

    summary = build_execution_summary(execution_id, record)
    if output_format == "markdown":
        return PlainTextResponse(
            render_summary_markdown(summary), media_type="text/markdown; charset=utf-8"
        )
    return summary
