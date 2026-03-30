import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import reload_mutable_settings, settings
from app.models import DatasetDescriptor, ExecutionRecord, ExecutionResponse
from app.orchestrator import create_tnlcm_execution, get_execution, start_elcm_phase

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


def verify_api_key(x_api_key: str = Header(...)) -> None:
    """Autenticacion minima por API key."""
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="API key invalida")


def to_execution_response(record: ExecutionRecord) -> ExecutionResponse:
    return ExecutionResponse(
        execution_id=record.execution_id,
        status=record.status,
        message=record.message,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"DaaS Orchestrator arrancando en modo {settings.app_env}")
    yield
    logger.info("DaaS Orchestrator apagandose")


app = FastAPI(
    title="DaaS Orchestrator",
    description="Capa de automatizacion para generacion de datasets en redes 5G/6G",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "env": settings.app_env}


@app.post(
    "/config/reload",
    tags=["config"],
    dependencies=[Depends(verify_api_key)],
)
async def post_reload_config():
    """Recarga en caliente solo variables de configuracion mutables."""
    try:
        result = reload_mutable_settings()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    logging.getLogger().setLevel(settings.log_level)
    logger.info("Configuracion recargada. Campos actualizados: %s", result["updated_fields"])
    return {
        "status": "reloaded",
        "updated_fields": result["updated_fields"],
        "non_reloadable_fields": result["non_reloadable_fields"],
    }


@app.post(
    "/executions",
    response_model=ExecutionResponse,
    status_code=202,
    tags=["executions"],
    dependencies=[Depends(verify_api_key)],
)
async def post_execution(descriptor: DatasetDescriptor):
    """Alias compatible: inicia fase TNLCM."""
    logger.info(f"Nueva ejecucion TNLCM solicitada: {descriptor.infrastructure.name}")
    record = await create_tnlcm_execution(descriptor)
    return to_execution_response(record)


@app.post(
    "/executions/tnlcm",
    response_model=ExecutionResponse,
    status_code=202,
    tags=["executions"],
    dependencies=[Depends(verify_api_key)],
)
async def post_execution_tnlcm(descriptor: DatasetDescriptor):
    """Inicia solo la fase TNLCM (deploy y espera de VPN manual)."""
    logger.info(f"Nueva ejecucion TNLCM solicitada: {descriptor.infrastructure.name}")
    record = await create_tnlcm_execution(descriptor)
    return to_execution_response(record)


@app.post(
    "/executions/{execution_id}/elcm",
    response_model=ExecutionResponse,
    status_code=202,
    tags=["executions"],
    dependencies=[Depends(verify_api_key)],
)
async def post_execution_elcm(execution_id: str):
    """Dispara la fase ELCM y cleanup final de la TN."""
    try:
        record = await start_elcm_phase(execution_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return to_execution_response(record)


@app.get(
    "/executions/{execution_id}",
    response_model=ExecutionResponse,
    tags=["executions"],
    dependencies=[Depends(verify_api_key)],
)
async def get_execution_status(execution_id: str):
    """Devuelve el estado resumido de una ejecucion."""
    record = get_execution(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Ejecucion no encontrada")
    return to_execution_response(record)


@app.get(
    "/executions/{execution_id}/detail",
    response_model=ExecutionRecord,
    tags=["executions"],
    dependencies=[Depends(verify_api_key)],
)
async def get_execution_detail(execution_id: str):
    """Devuelve el registro completo (incluye artifacts y error)."""
    record = get_execution(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Ejecucion no encontrada")
    return record


@app.post(
    "/tnlcm/token/refresh",
    tags=["auth"],
    dependencies=[Depends(verify_api_key)],
)
def refresh_tnlcm_token():
    """Genera token TNLCM con user/password de .env y lo guarda en .env."""
    from app.tnlcm import login_tnlcm_and_persist_token

    try:
        token = login_tnlcm_and_persist_token()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        detail = f"TNLCM login failed: HTTP {exc.response.status_code}"
        raise HTTPException(status_code=502, detail=detail)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not refresh TNLCM token: {exc}")

    # Return only a safe preview, not the full token.
    preview = f"{token[:12]}...{token[-6:]}" if len(token) > 20 else "[token-set]"
    return {
        "message": "TNLCM token refreshed and saved in .env",
        "token_preview": preview,
    }


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Error no controlado en {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})
