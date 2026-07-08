import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Query
from fastapi.responses import JSONResponse

from app.config import reload_mutable_settings, settings
from app.models import (
    DatasetDescriptor,
    ExecutionRecord,
    ExecutionResponse,
    InfrastructureConfig,
)
from app.orchestrator import (
    TnlcmDeploymentInProgressError,
    create_tnlcm_execution,
    get_execution,
    start_elcm_phase,
)
from app.utils.telemetry import format_duration_display, telemetry
from app.utils.component_contract import extract_component_template_values

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


def main() -> None:
    """Punto de entrada de consola (`daas-orchestrator`) para arrancar Uvicorn."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_env == "dev",
    )


@app.post(
    "/refresh",
    tags=["config"],
    dependencies=[Depends(verify_api_key)],
)
async def post_refresh_config():
    """Recarga en caliente solo variables de configuracion mutables.

    Nota: este endpoint se renombró desde /login a /refresh.
    """
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
    "/register",
    tags=["auth"],
)
async def post_register(
    username: str = Query(..., description="username, required"),
    password: str = Query(..., description="password, required"),
    org: str | None = Query(None, description="org, optional"),
    email: str | None = Query(None, description="email, optional"),
) -> dict[str, str]:
    """Registro que delega en TNLCM y luego realiza login para obtener token.

    Todos los parámetros se reciben por query string: ?username=x&password=y&email=a&org=b
    - `username` y `password` son obligatorios.
    - `email` y `org` son opcionales.

    El endpoint hace POST a TNLCM /api/v1/user/register (sin autenticación)
    enviando en el body JSON:

    {
      "email": "...",
      "username": "...",
      "password": "...",
      "org": "..."
    }

    Luego realiza login para obtener los tokens, los guarda en memoria y devuelve
    una respuesta con token enmascarado.
    """
    # Validar obligatorios (FastAPI ya fuerza Body(...), pero reforzamos)
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")

    from app.tnlcm import TNLCM_LOGIN_TIMEOUT_SECONDS, _response_error_detail
    import app.tnlcm

    # Construir body exactamente como TNLCM espera
    register_payload: dict = {}
    # TNLCM example includes email even if optional; include fields only when present
    if email is not None:
        register_payload["email"] = email
    register_payload["username"] = username
    register_payload["password"] = password
    if org is not None:
        register_payload["org"] = org

    telemetry.increment_counter(
        "requests_total", labels={"service": "auth", "operation": "register"}
    )
    telemetry.log_event("info", "user.register.request", username=username, email=email)

    async with httpx.AsyncClient(timeout=TNLCM_LOGIN_TIMEOUT_SECONDS) as client:
        # 1) Registrar en TNLCM (no auth)
        try:
            resp = await client.post(
                f"{settings.tnlcm_url}/api/v1/user/register",
                json=register_payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _response_error_detail(exc.response) or "unknown"
            telemetry.increment_counter(
                "errors_total",
                labels={
                    "service": "auth",
                    "operation": "register",
                    "error_type": str(exc.response.status_code),
                },
            )
            raise HTTPException(
                status_code=exc.response.status_code, detail=f"TNLCM register failed: {detail}"
            ) from exc
        except httpx.TimeoutException as exc:
            telemetry.increment_counter(
                "errors_total", labels={"service": "auth", "operation": "register"}
            )
            raise HTTPException(
                status_code=504, detail="Timeout contacting TNLCM register endpoint"
            ) from exc

        # 2) Login with the newly created credentials to get tokens
        try:
            login_resp = await client.post(
                f"{settings.tnlcm_url}/api/v1/user/login",
                auth=(username, password),
                headers={"Accept": "application/json"},
            )
            login_resp.raise_for_status()
            response_data = login_resp.json()
        except httpx.HTTPStatusError as exc:
            detail = _response_error_detail(exc.response) or "unknown"
            telemetry.increment_counter(
                "errors_total",
                labels={
                    "service": "auth",
                    "operation": "login",
                    "error_type": str(exc.response.status_code),
                },
            )
            raise HTTPException(
                status_code=502, detail=f"TNLCM login failed after register: {detail}"
            ) from exc
        except httpx.TimeoutException as exc:
            telemetry.increment_counter(
                "errors_total", labels={"service": "auth", "operation": "login"}
            )
            raise HTTPException(
                status_code=504, detail="Timeout contacting TNLCM login endpoint"
            ) from exc

    access_token = (
        response_data.get("access_token")
        or response_data.get("token")
        or (response_data.get("data") or {}).get("access_token")
    )
    refresh_token = response_data.get("refresh_token") or (response_data.get("data") or {}).get(
        "refresh_token"
    )

    if access_token is None:
        raise HTTPException(
            status_code=502, detail=f"TNLCM login did not return access_token: {response_data}"
        )

    app.tnlcm._tnlcm_access_token = str(access_token).strip()
    if refresh_token:
        app.tnlcm._tnlcm_refresh_token = str(refresh_token).strip()

    token_preview = (
        f"{app.tnlcm._tnlcm_access_token[:12]}...{app.tnlcm._tnlcm_access_token[-6:]}"
        if len(app.tnlcm._tnlcm_access_token) > 20
        else "[token-set]"
    )

    telemetry.log_event("info", "user.register.completed", username=username)

    return {"status": "ok", "token_preview": token_preview}


@app.post(
    "/executions",
    response_model=ExecutionResponse,
    status_code=202,
    tags=["executions"],
    dependencies=[Depends(verify_api_key)],
)
async def post_execution(descriptor: DatasetDescriptor):
    """Inicia una ejecución completa: TNLCM y opcionalmente ELCM (auto-start).

    Si descriptor.auto_start_elcm=true (por defecto), ELCM se inicia automáticamente
    al completar TNLCM. Establecer auto_start_elcm=false para control manual del flujo.
    """
    # Explicit validation: ensure client did not supply disallowed fields in components
    from app.utils.ytt_renderer import resolve_template_path, overlay_editable_fields_for_template
    from app.generators.tnlcm_overlay import InvalidDataDescriptorError

    def _validate_components_or_raise(infra: InfrastructureConfig) -> None:
        comps = infra.component or {}
        invalids: list[str] = []
        
        for comp_key, comp_values in (comps.items() if isinstance(comps, dict) else []):
            if not isinstance(comp_values, dict) or not comp_values:
                # empty dict or non-dict is acceptable (empty means include defaults)
                continue

            # Resolve template path for this component
            candidate = "base_tnlcm_descriptor.yaml" if comp_key == "base" else f"{comp_key}_sample_tnlcm_descriptor.yaml"
            comp_template = resolve_template_path(candidate, category="TNLCM")

            if comp_template is None:
                invalids.append(f"component.{comp_key}: template not found")
                continue

            # Obtener campos editables del overlay
            allowed = overlay_editable_fields_for_template(str(comp_template), category="TNLCM")
            editable_by_section: dict[str, set[str]] = {
                section: set(fields) for section, fields in allowed.items()
            }

            # Usar extractor centralizado para normalizar y validar campos
            _, component_invalids = extract_component_template_values(
                comp_key=comp_key,
                comp_values=comp_values,
                editable_by_section=editable_by_section,
            )
            invalids.extend(component_invalids)

        if invalids:
            raise HTTPException(status_code=400, detail={"invalid_fields": invalids})

    # Run validation before proceeding
    try:
        _validate_components_or_raise(descriptor.infrastructure)
    except HTTPException:
        raise
    except InvalidDataDescriptorError as exc:
        raise HTTPException(status_code=400, detail={"invalid_fields": exc.invalid_fields})

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
    record = None
    logger.info(
        f"Nueva ejecucion solicitada (auto_elcm={descriptor.auto_start_elcm}): {descriptor.infrastructure.name}"
    )
    try:
        record = await create_tnlcm_execution(descriptor)
        return to_execution_response(record)
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


@app.post(
    "/executions/{execution_id}/elcm",
    response_model=ExecutionResponse,
    status_code=202,
    tags=["executions"],
    dependencies=[Depends(verify_api_key)],
)
async def post_execution_elcm(execution_id: str):
    """Dispara manualmente la fase ELCM y cleanup final de la TN.

    Útil cuando descriptor.auto_start_elcm=false. Si ELCM ya está en progreso,
    devuelve el estado actual sin error.
    """
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
    "/login",
    tags=["auth"],
    dependencies=[Depends(verify_api_key)],
)
def refresh_tnlcm_token():
    """Genera token TNLCM con user/password de .env y lo guarda en memoria."""
    from app.tnlcm import login_tnlcm_and_persist_token

    try:
        token = login_tnlcm_and_persist_token()
        logger.info("TNLCM token refreshed and successfully logged in")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail=(
                "TNLCM login timeout after 20 seconds. "
                "Verifica la conectividad y que la VPN este activa."
            ),
        )
    except httpx.HTTPStatusError as exc:
        upstream_detail = ""
        try:
            payload = exc.response.json()
            if isinstance(payload, dict):
                upstream_detail = str(
                    payload.get("message")
                    or payload.get("detail")
                    or payload.get("error")
                    or payload
                )
            else:
                upstream_detail = str(payload)
        except Exception:
            upstream_detail = (exc.response.text or "").strip()

        detail = (
            f"TNLCM login failed: HTTP {exc.response.status_code}. "
            f"Backend error: {upstream_detail or 'unknown'}"
        )
        raise HTTPException(status_code=502, detail=detail)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not refresh TNLCM token: {exc}")

    # Return only a safe preview, not the full token.
    preview = f"{token[:12]}...{token[-6:]}" if len(token) > 20 else "[token-set]"
    return {
        "message": "TNLCM token refreshed and stored in memory",
        "token_preview": preview,
    }


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Error no controlado en {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})
