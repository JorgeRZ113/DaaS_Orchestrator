from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from app.utils.ytt_renderer import overlay_editable_fields_for_template


class ExecutionState(str, Enum):
    pending = "PENDING"
    validating = "VALIDATING"
    deploying = "DEPLOYING"
    tn_ready = "TN_READY"
    running_experiment = "RUNNING_EXPERIMENT"
    collecting = "COLLECTING"
    destroying = "DESTROYING"
    destroyed = "DESTROYED"
    # Estado legacy: se conserva para poder deserializar executions.json
    # antiguos, pero el pipeline ya no lo produce (ver TN_READY/DESTROYED).
    completed = "COMPLETED"
    failed = "FAILED"
    cancelled = "CANCELLED"


class RegisterRequest(BaseModel):
    email: str
    username: str
    password: str
    org: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    access_token: str


class TrialNetworkCreateRequest(BaseModel):
    descriptor: Dict[str, Any] = Field(
        ..., description="Descriptor de TNLCM para crear la Trial Network"
    )
    library_reference_type: str = Field(
        ..., description="Tipo de referencia en la libreria de TNLCM"
    )
    library_reference_value: str = Field(
        ..., description="Valor de referencia en la libreria de TNLCM"
    )
    tn_id: Optional[str] = Field(default=None, description="Identificador opcional de la TN")


class TNIdRequest(BaseModel):
    tn_id: str


class ActivateRequest(TNIdRequest):
    jenkins_deploy_pipeline: Optional[str] = Field(
        default=None,
        description="Pipeline opcional para activar/desplegar componentes",
    )


class InfrastructureConfig(BaseModel):
    name: str = Field(..., description="Nombre del escenario o TN")
    descriptor_path: Optional[str] = Field(
        default=None,
        description="Ruta o referencia al descriptor que consumira TNLCM",
    )
    component: Optional[Dict[str, Dict[str, Any]]] = Field(
        default=None,
        description="Componentes y sus valores (ej: component.base para plantilla base)",
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Parametros extra para el despliegue",
    )

    def tnlcm_template_ref(self) -> str | None:
        """Devuelve la referencia seleccionada para el template TNLCM."""
        for key in ("template_tnlcm", "template", "descriptor", "descriptor_path"):
            value = self.parameters.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if self.descriptor_path:
            return self.descriptor_path.strip()
        return None

    def tnlcm_data_values(self, template_ref: str | None = None) -> dict[str, Any]:
        """Return the data values to populate overlays.

        If `template_ref` is provided, only values that match sections used by that
        template's overlay chain are returned. Additionally, when filtering by
        template, only keys inside a section that are declared as editable (i.e.
        have default value "" in the overlay) are accepted.

        If user provides fields directly (not grouped under section), they are
        automatically grouped under their corresponding section based on the overlay.

        If `template_ref` is None the behaviour is backwards compatible: merge
        all component subsections into a single dict (same as previous logic).
        """
        # If caller asks for values for a specific template, filter component values
        if template_ref and self.component and isinstance(self.component, dict):
            allowed = overlay_editable_fields_for_template(template_ref, category="TNLCM")

            # Build reverse mapping: field -> section
            field_to_section: dict[str, str] = {}
            for section, fields in allowed.items():
                for field in fields:
                    field_to_section[field] = section

            merged: dict[str, Any] = {}
            # Iterate user-provided components and pick section keys that the
            # overlay actually allows to override (and only the editable fields)
            for comp_values in self.component.values():
                if not isinstance(comp_values, dict):
                    continue

                # Normalize: if user sends fields directly (not in a sub-dict),
                # group them under their section
                normalized: dict[str, dict[str, Any]] = {}
                ungrouped_fields: dict[str, Any] = {}

                for section, section_values in comp_values.items():
                    # If value is a dict, treat it as a section
                    if section not in allowed:
                        # Could be a field sent directly, or unrecognized section -> skip or collect
                        if isinstance(section_values, dict):
                            # It's a dict but not a known section -> skip
                            continue
                        # It's a scalar value, could be an editable field
                        ungrouped_fields[section] = section_values
                        continue
                    if not isinstance(section_values, dict):
                        # Value is not a dict but section name matches -> skip
                        # (section should contain a dict of fields)
                        continue

                    # It's a known section with a dict of fields
                    editable_fields = allowed.get(section, set())
                    filtered = {k: v for k, v in section_values.items() if k in editable_fields}
                    if filtered:
                        normalized.setdefault(section, {}).update(filtered)

                # Group ungrouped scalar fields under their sections
                for field_name, field_value in ungrouped_fields.items():
                    if field_name in field_to_section:
                        section = field_to_section[field_name]
                        normalized.setdefault(section, {})[field_name] = field_value

                # Merge normalized data
                for section, fields in normalized.items():
                    merged.setdefault(section, {}).update(fields)

            if merged:
                return merged

        # Backwards-compatible fallback: search in parameters for explicit data blocks
        candidate: Any = None
        for key in ("data_descriptor", "values", "data"):
            value = self.parameters.get(key)
            if isinstance(value, dict):
                candidate = value
                break
        if candidate is None:
            candidate = self.parameters

        if not isinstance(candidate, dict):
            return {}

        reserved = {
            "template_tnlcm",
            "template",
            "descriptor",
            "descriptor_path",
            "data_descriptor",
            "values",
            "data",
        }
        return {key: value for key, value in candidate.items() if key not in reserved}


class ExperimentConfig(BaseModel):
    name: str = Field(
        ..., description="Nombre local del experimento para asociarlo luego con experiment_id"
    )
    testcase_paths: List[str] = Field(
        default_factory=list,
        description="Lista de rutas/referencias de TestCases para ejecutar en orden",
    )
    ues_paths: List[str] = Field(
        default_factory=list,
        description="Lista de rutas/referencias de UEs a usar en el experimento",
    )


# Formatos de entrega soportados por dataset.output. El esquema los acepta todos;
# la disponibilidad REAL en el runtime se controla aparte en el orquestador
# (IMPLEMENTED_DATASET_OUTPUTS), activándolos de forma incremental.
DatasetOutput = Literal["logs", "csv", "dashboard", "raw"]


class DatasetRequest(BaseModel):
    output: List[DatasetOutput] = Field(
        default_factory=lambda: ["logs"],
        description=(
            "Formato(s) de entrega del dataset. Acepta un único nombre ('logs') "
            "o una lista (['logs', 'csv']). Valores válidos: logs, csv, dashboard, raw."
        ),
    )

    @field_validator("output", mode="before")
    @classmethod
    def _normalize_output(cls, value: Any) -> Any:
        """Aceptar string suelto o lista y normalizar a lista deduplicada.

        - Un string se envuelve en lista ('logs' -> ['logs']).
        - Cada nombre se recorta y se pasa a minúsculas (case-insensitive).
        - Se eliminan duplicados conservando el orden de aparición.

        La pertenencia al conjunto permitido la valida el tipo Literal DESPUÉS de
        esta coerción: un valor desconocido (p. ej. 'zip') hace fallar la
        validación de Pydantic.
        """
        if value is None:
            return ["logs"]
        if isinstance(value, str):
            items: list[Any] = [value]
        elif isinstance(value, (list, tuple)):
            items = list(value)
        else:
            # Tipo inesperado: se deja pasar para que Pydantic emita el error.
            return value

        normalized: list[Any] = []
        seen: set[Any] = set()
        for item in items:
            key = item.strip().lower() if isinstance(item, str) else item
            if key in seen:
                continue
            seen.add(key)
            normalized.append(key)

        if not normalized:
            raise ValueError("dataset.output must contain at least one delivery format")
        return normalized

    def wants(self, kind: str) -> bool:
        """True si `kind` está entre los formatos de entrega solicitados."""
        return kind in self.output


class ElcmExperimentRequest(BaseModel):
    experiment: ExperimentConfig
    dataset: DatasetRequest = Field(
        default_factory=DatasetRequest,
        description=(
            "Formato(s) de entrega del dataset para ESTE experimento. Cada "
            "llamada a /elcm puede pedir una salida distinta; por defecto 'logs'."
        ),
    )


class DatasetDescriptor(BaseModel):
    infrastructure: InfrastructureConfig
    experiment: Optional[ExperimentConfig] = Field(
        default=None,
        description="Experimento inicial; obligatorio solo si auto_start_elcm=True",
    )
    dataset: DatasetRequest = Field(default_factory=DatasetRequest)
    auto_start_elcm: bool = Field(
        default=True, description="Si True, inicia automáticamente ELCM al completar TNLCM"
    )
    ephemeral_tn: bool = Field(
        default=False,
        description=(
            "Si True (y auto_start_elcm=True), la TN es de un solo uso: se borra "
            "automáticamente tras el primer experimento. Ignorado si auto_start_elcm=False."
        ),
    )

    @model_validator(mode="after")
    def _require_experiment_for_auto_start(self) -> "DatasetDescriptor":
        """Con auto-start el experimento inicial debe venir en el descriptor."""
        if self.auto_start_elcm and self.experiment is None:
            raise ValueError("experiment is required when auto_start_elcm is true")
        return self

    def tnlcm_template_ref(self) -> str | None:
        return self.infrastructure.tnlcm_template_ref()

    def tnlcm_data_values(self, template_ref: str | None = None) -> dict[str, Any]:
        return self.infrastructure.tnlcm_data_values(template_ref=template_ref)


class ExperimentRun(BaseModel):
    """Registro de un experimento individual ejecutado sobre la TN."""

    name: str
    elcm_execution_id: Optional[str] = None
    status: Literal["RUNNING", "FINISHED", "FAILED"] = "RUNNING"
    # Formatos de entrega solicitados para este experimento concreto (dataset.output
    # del body de /elcm). Se recolectan en artifacts/<id>/result/<experimento>/.
    dataset_output: List[str] = Field(default_factory=lambda: ["logs"])
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class ExecutionRecord(BaseModel):
    execution_id: str
    status: ExecutionState
    message: str = ""
    ephemeral_tn: bool = False
    # Formatos de entrega solicitados (dataset.output), fijados al crear la
    # ejecución. La fase ELCM los consulta para saber qué recolectar/inyectar.
    dataset_output: List[str] = Field(default_factory=lambda: ["logs"])
    experiments: List[ExperimentRun] = Field(default_factory=list)
    tn_id: Optional[str] = None
    vpn_interface: Optional[str] = None
    vpn_conf_path: Optional[str] = None
    vpn_status: Optional[str] = None
    vpn_error: Optional[str] = None
    experiment_id: Optional[str] = None
    experiment_ids: List[str] = Field(default_factory=list)
    elcm_execution_id: Optional[str] = None  # ID de la ejecución en ELCM
    elcm_base_url: Optional[str] = None
    artifacts: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    _execution_timer: Any = PrivateAttr(default=None)


class ExecutionResponse(BaseModel):
    execution_id: str
    status: ExecutionState
    message: str = ""
    tn_id: Optional[str] = None


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
