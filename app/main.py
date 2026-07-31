import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Query
from fastapi.responses import JSONResponse

from app import tnlcm
from app.config import reload_mutable_settings, settings
from app.generators.tnlcm_overlay import InvalidDataDescriptorError
from app.health import check_components, check_services
from app.models import (
    ComponentsHealthResponse,
    DatasetDescriptor,
    ElcmExperimentRequest,
    ExecutionRecord,
    ExecutionResponse,
    InfrastructureConfig,
    ServicesHealthResponse,
)
from app.orchestrator import (
    ExecutionConflictError,
    ExecutionNotFoundError,
    TnlcmDeploymentInProgressError,
    create_tnlcm_execution,
    get_execution,
    start_elcm_phase,
    start_tn_teardown,
)
from app.utils.component_contract import extract_component_template_values
from app.utils.telemetry import format_duration_display, telemetry
from app.utils.ytt_renderer import (
    overlay_editable_fields_for_template,
    resolve_template_path,
)

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
        tn_id=record.tn_id,
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


@app.get(
    "/health/services",
    response_model=ServicesHealthResponse,
    tags=["health"],
)
async def health_services() -> ServicesHealthResponse:
    """Liveness del propio orquestador y de TNLCM (sin auth)."""
    result = await check_services()
    return ServicesHealthResponse(**result)


@app.get(
    "/health/components",
    response_model=ComponentsHealthResponse,
    tags=["health"],
    dependencies=[Depends(verify_api_key)],
)
async def health_components() -> ComponentsHealthResponse:
    """Health HTTP de los servicios fijos (requiere API key).

    Comprueba InfluxDB, Grafana, Prometheus y ELCM según el diccionario estático
    `KNOWN_SERVICES` (IP/puerto/ruta). No necesita tn_id ni token.
    """
    result = await check_components()
    return ComponentsHealthResponse(**result)


def main() -> None:
    """Punto de entrada de consola (`daas-orchestrator`) para arrancar Uvicorn."""
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

    async with httpx.AsyncClient(timeout=tnlcm.TNLCM_LOGIN_TIMEOUT_SECONDS) as client:
        # 1) Registrar en TNLCM (no auth)
        try:
            resp = await client.post(
                f"{settings.tnlcm_url}/api/v1/user/register",
                json=register_payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = tnlcm._response_error_detail(exc.response) or "unknown"
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
            detail = tnlcm._response_error_detail(exc.response) or "unknown"
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

    tnlcm._tnlcm_access_token = str(access_token).strip()
    if refresh_token:
        tnlcm._tnlcm_refresh_token = str(refresh_token).strip()

    token_preview = (
        f"{tnlcm._tnlcm_access_token[:12]}...{tnlcm._tnlcm_access_token[-6:]}"
        if len(tnlcm._tnlcm_access_token) > 20
        else "[token-set]"
    )

    telemetry.log_event("info", "user.register.completed", username=username)

    return {"status": "ok", "token_preview": token_preview}


def _collect_empty_string_paths(value: Any, prefix: str = "") -> list[str]:
    """Recorrer recursivamente el body y devolver las rutas de todos string vacío.

    Un valor "" (o solo espacios) casi siempre es un campo que el cliente dejó a
    medias: o lo rellena con un valor real o lo elimina del body. Se devuelven
    rutas tipo "infrastructure.component.base.grafana_password" para que el
    cliente sepa exactamente qué corregir antes de reenviar el POST.

    Args:
        value: Nodo del body (dict, list o escalar) a inspeccionar.
        prefix: Ruta acumulada hasta este nodo (dot-path).

    Returns:
        Lista de rutas (dot-path) donde se encontró un string vacío.
    """
    empty_paths: list[str] = []

    if isinstance(value, str):
        if value.strip() == "":
            empty_paths.append(prefix or "<root>")
    elif isinstance(value, dict):
        for key, sub_value in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            empty_paths.extend(_collect_empty_string_paths(sub_value, child_prefix))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            empty_paths.extend(_collect_empty_string_paths(item, f"{prefix}[{index}]"))

    return empty_paths


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

    dataset.output acepta un nombre o una lista combinable de: logs, csv, dashboard,
    raw. Las respuestas del dataset se guardan en artifacts/<execution_id>/result/.
    """

    # Explicit validation: ensure client did not supply disallowed fields in components
    def _validate_components_or_raise(infra: InfrastructureConfig) -> None:
        comps = infra.component or {}
        invalids: list[str] = []

        for comp_key, comp_values in (comps.items() if isinstance(comps, dict) else []):
            if not isinstance(comp_values, dict) or not comp_values:
                # empty dict or non-dict is acceptable (empty means include defaults)
                continue

            # Resolve template path for this component
            candidate = (
                "base_tnlcm_descriptor.yaml"
                if comp_key == "base"
                else f"{comp_key}_sample_tnlcm_descriptor.yaml"
            )
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

    # Rechazo temprano (Fail-Fast): si el cliente envió algún string vacío ("")
    # en cualquier parte del body, no ejecutamos nada. Debe reenviar un POST bien
    # formado, rellenando el valor o eliminando el campo. Solo inspeccionamos lo
    # que el cliente envió (exclude_unset) para no marcar defaults del servidor.
    empty_fields = _collect_empty_string_paths(descriptor.model_dump(exclude_unset=True))
    if empty_fields:
        raise HTTPException(
            status_code=400,
            detail={
                "empty_fields": sorted(empty_fields),
                "message": (
                    'Algunos campos llegaron vacíos (""). Rellénalos con un valor '
                    "o elimínalos del body y reenvía el POST."
                ),
            },
        )

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
async def post_execution_elcm(execution_id: str, request: ElcmExperimentRequest):
    """Lanza un experimento ELCM sobre la TN viva de la ejecucion.

    Puede llamarse tantas veces como experimentos se quieran ejecutar (uno a
    la vez). Cada experimento debe tener un nombre unico dentro de la TN.

    El body admite `dataset.output` propio: cada experimento puede pedir una
    salida de datos distinta (logs/csv/dashboard/raw), que se recolecta en
    artifacts/<execution_id>/result/<experimento>/.

    Respuestas: 202 experimento aceptado; 404 la ejecucion no existe;
    409 hay un experimento en curso, la TN no esta lista o el nombre esta repetido.
    """
    try:
        record = start_elcm_phase(execution_id, request.experiment, request.dataset)
    except ExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ExecutionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return to_execution_response(record)


@app.delete(
    "/executions/{execution_id}/tn",
    response_model=ExecutionResponse,
    status_code=202,
    tags=["executions"],
    dependencies=[Depends(verify_api_key)],
)
async def delete_execution_tn(execution_id: str):
    """Dispara el bloque de borrado de la TN (deleted + purged) bajo demanda.

    La respuesta indica en `tn_id` que Trial Network se esta borrando.

    Respuestas: 202 borrado lanzado; 404 la ejecucion no existe o no tiene TN;
    409 hay un experimento en curso o el borrado ya se lanzo/completo.
    """
    try:
        record = start_tn_teardown(execution_id)
    except ExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ExecutionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
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
    try:
        token = tnlcm.login_tnlcm_and_persist_token()
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
