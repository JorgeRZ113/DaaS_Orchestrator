"""Cuerpos de respuesta de la API del orquestador.

Lo que sale por el cable. Se mantienen separados de `app.domain` a proposito:
cambiar la forma de una respuesta no debe obligar a tocar el modelo de negocio,
ni al reves.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord


class ExecutionResponse(BaseModel):
    """Respuesta de los endpoints de ciclo de vida.

    El desenlace lo dice el codigo HTTP (200 completo, 207 incompleto, 502 la
    fase fallo). Estos campos son el detalle para poder actuar: que TN quedo
    creada, si el tunel necesita montarse a mano y que fallo exactamente.
    """

    execution_id: str
    status: ExecutionState
    message: str = ""
    tn_id: Optional[str] = None
    vpn_status: Optional[str] = None
    error: Optional[str] = None


class ExecutionDetailResponse(ExecutionRecord):
    """El registro completo mas el estado que TNLCM reporta ahora mismo para la TN.

    Extiende el record en lugar de duplicarlo: el detalle siempre expuso el
    `ExecutionRecord` tal cual y el campo nuevo se suma a el, sin tocar el resto
    del contrato. `tn_state` no vive en el record a proposito, porque se
    quedaria viejo en cuanto TNLCM cambiara la TN por su cuenta: se pregunta en
    cada consulta del detalle.
    """

    # Estado normalizado (minusculas) del campo `state` que devuelve TNLCM:
    # `created`, `activated`, `destroyed`... Queda a null si la ejecucion aun no
    # tiene TN, si TNLCM ya no la conoce (404) o si no se pudo consultar.
    tn_state: Optional[str] = None


class ExecutionStep(BaseModel):
    """Un paso del resumen legible: que se hizo, cuanto tardo y como acabo.

    `duration` viene ya formateado en lenguaje natural ("3 min 57 s") para
    pintarlo tal cual; `duration_seconds` se conserva para ordenar o graficar.
    """

    step: str
    status: Literal["ok", "error", "running", "pending", "skipped"]
    duration: Optional[str] = None
    duration_seconds: Optional[float] = None
    attempts: Optional[int] = None
    detail: Optional[str] = None


class ExecutionSummary(BaseModel):
    """Vista de una ejecucion pensada para el experimentador, no para el programador.

    La construye `app.observability.execution_summary` a partir de la telemetria y del
    `ExecutionRecord`; es lo que devuelve `GET /executions/{id}/summary` y lo
    que se persiste como `summary.json`.
    """

    execution_id: str
    status: str
    state: ExecutionState
    outcome: Literal["ok", "error", "running"]
    message: str = ""
    network: Optional[str] = None
    vpn_status: Optional[str] = None
    total_duration: Optional[str] = None
    total_duration_seconds: Optional[float] = None
    experiments_total: int = 0
    experiments_successful: int = 0
    steps: List[ExecutionStep] = Field(default_factory=list)
    technical_steps: List[ExecutionStep] = Field(default_factory=list)
    results: List[str] = Field(default_factory=list)
    dashboards: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    what_went_wrong: Optional[str] = None
    generated_at: str


class ServiceHealth(BaseModel):
    """Estado de liveness de un servicio individual (orquestador o TNLCM)."""

    alive: bool
    url: Optional[str] = None


class ServicesHealthResponse(BaseModel):
    """Respuesta del health de servicios: orquestador propio + TNLCM."""

    status: Literal["ok", "fallen"]
    orchestrator: ServiceHealth
    tnlcm: ServiceHealth


class ServiceProbe(BaseModel):
    """Resultado del health HTTP de un servicio fijo."""

    # influxdb | grafana | prometheus | elcm
    service: str
    healthy: bool


class ComponentsHealthResponse(BaseModel):
    """Salud de los servicios fijos monitorizables (InfluxDB/Grafana/Prometheus/ELCM)."""

    status: Literal["ok", "fallen"]
    services: List[ServiceProbe] = Field(default_factory=list)
    note: str = ""
