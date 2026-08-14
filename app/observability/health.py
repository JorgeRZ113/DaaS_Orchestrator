"""Comprobaciones de salud (health checks) del orquestador y sus dependencias.

Expone dos niveles:
  * `check_services`: liveness del propio orquestador y de TNLCM.
  * `check_components`: health HTTP de los servicios fijos monitorizables
    (InfluxDB, Grafana, Prometheus y ELCM), leyendo su IP/puerto/ruta de un
    diccionario estático `KNOWN_SERVICES`. Sin parseo del report ni token.

Los sondeos HTTP usan `httpx.AsyncClient` con timeout explícito (§8.1) y corren
en paralelo con `asyncio.gather`.
"""

import asyncio
from typing import Any, Optional

import httpx

from app.core.config import settings

# Timeout explícito (nunca None): sonda HTTP a TNLCM y a cada servicio.
HEALTH_HTTP_TIMEOUT = 5.0

# Servicios fijos monitorizables. Fuente única de información: IP, puerto y ruta
# de health de cada uno. `healthy_statuses=None` => cualquier respuesta HTTP se
# interpreta como "vivo" (el servicio está escuchando).
#   * InfluxDB   -> GET /health       (200/204)
#   * Grafana    -> GET /api/health   (200)
#   * Prometheus -> GET /-/healthy    (200)
#   * ELCM       -> GET / (backend)   (cualquier respuesta = vivo)
# NOTA: ajusta las IPs a tu despliegue. La VM de monitorización agrupa
# InfluxDB/Grafana/Prometheus; ELCM va en su propia VM.
KNOWN_SERVICES: dict[str, dict[str, Any]] = {
    "influxdb": {
        "ip": "192.168.199.2",
        "port": 8086,
        "path": "/health",
        "healthy_statuses": {200, 204},
    },
    "grafana": {
        "ip": "192.168.199.2",
        "port": 3000,
        "path": "/api/health",
        "healthy_statuses": {200},
    },
    "prometheus": {
        "ip": "192.168.199.2",
        "port": 9090,
        "path": "/-/healthy",
        "healthy_statuses": {200},
    },
    "elcm": {
        "ip": "192.168.199.3",
        "port": 5001,
        "path": "/",
        "healthy_statuses": None,
    },
}

# Nota adjunta al health de componentes: aviso sobre alcanzabilidad.
_COMPONENTS_NOTE = (
    "El health HTTP requiere alcanzar la IP:puerto de cada servicio; "
    "normalmente solo son accesibles con el túnel WireGuard activo, "
    "por lo que 'healthy=false' sin VPN puede ser un falso negativo."
)


async def check_tnlcm_alive() -> dict[str, Any]:
    """Sondea la URL base de TNLCM.

    Cualquier respuesta HTTP (incluidos 401/404) se interpreta como "vivo",
    porque implica que el servicio está escuchando. Solo los errores de
    conexión/timeout se consideran caída.
    """
    url = settings.tnlcm_url
    try:
        async with httpx.AsyncClient(timeout=HEALTH_HTTP_TIMEOUT) as client:
            await client.get(url)
        return {"alive": True, "url": url}
    except httpx.HTTPError:
        # Timeout, ConnectError, ConnectTimeout, etc.: TNLCM no responde.
        return {"alive": False, "url": url}


async def check_services() -> dict[str, Any]:
    """Compone el liveness del orquestador propio y de TNLCM."""
    orchestrator = {"alive": True, "url": None}
    tnlcm = await check_tnlcm_alive()
    status = "ok" if orchestrator["alive"] and tnlcm["alive"] else "fallen"
    return {"status": status, "orchestrator": orchestrator, "tnlcm": tnlcm}


async def probe_http_service(url: str, healthy_statuses: Optional[set[int]]) -> bool:
    """Hace GET al endpoint de health de un servicio y evalúa el resultado.

    Si `healthy_statuses` es None, cualquier respuesta HTTP se considera sana
    (el servicio está escuchando). En caso contrario, sano = código en el set.
    """
    try:
        async with httpx.AsyncClient(timeout=HEALTH_HTTP_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        # Timeout, ConnectError, etc.: el servicio no responde.
        return False

    if healthy_statuses is None:
        return True
    return response.status_code in healthy_statuses


async def _probe_service(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Sondea un servicio del registro construyendo su URL de health."""
    url = f"http://{cfg['ip']}:{cfg['port']}{cfg['path']}"
    healthy = await probe_http_service(url, cfg["healthy_statuses"])
    return {"service": name, "healthy": healthy}


async def check_components() -> dict[str, Any]:
    """Health HTTP de los servicios fijos definidos en `KNOWN_SERVICES`."""
    services = await asyncio.gather(
        *(_probe_service(name, cfg) for name, cfg in KNOWN_SERVICES.items())
    )
    services = list(services)
    status = "ok" if all(service["healthy"] for service in services) else "fallen"
    return {"status": status, "services": services, "note": _COMPONENTS_NOTE}
