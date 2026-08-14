"""Health checks del orquestador y de los servicios monitorizables."""

from fastapi import APIRouter, Depends

from app.api.deps import verify_api_key
from app.api.schemas.responses import ComponentsHealthResponse, ServicesHealthResponse
from app.observability.health import check_components, check_services

router = APIRouter(tags=["health"])


@router.get("/health/services", response_model=ServicesHealthResponse)
async def health_services() -> ServicesHealthResponse:
    """Liveness del propio orquestador y de TNLCM (sin auth)."""
    result = await check_services()
    return ServicesHealthResponse(**result)


@router.get(
    "/health/components",
    response_model=ComponentsHealthResponse,
    dependencies=[Depends(verify_api_key)],
)
async def health_components() -> ComponentsHealthResponse:
    """Health HTTP de los servicios fijos (requiere API key).

    Comprueba InfluxDB, Grafana, Prometheus y ELCM según el diccionario estático
    `KNOWN_SERVICES` (IP/puerto/ruta). No necesita tn_id ni token.
    """
    result = await check_components()
    return ComponentsHealthResponse(**result)
