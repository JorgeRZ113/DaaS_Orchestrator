"""El DatasetDescriptor: lo que el usuario envia en `POST /executions`.

Modela los tres bloques de la peticion -- `infrastructure` (que desplegar),
`experiment` (que ejecutar) y `dataset` (como entregar los datos) -- y las reglas
que los relacionan. `infrastructure.component` se valida contra los overlays en
tiempo de ejecucion, no contra un esquema estatico: de ahi la dependencia de
`app.rendering.overlays`.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.rendering.overlays import overlay_editable_fields_for_template


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

    @field_validator("component", mode="before")
    @classmethod
    def _empty_component_means_defaults(cls, value: Any) -> Any:
        """Aceptar un componente sin valores como «despliegalo con sus defaults».

        En JSON eso se escribe `"ueransim_both": {}`; en YAML lo natural es dejar
        la clave vacia, y eso llega como None. Sin esta coercion el modelo
        respondia 422 por un caso que `validate_components_or_raise` ya da por
        bueno explicitamente ("un valor no-dict significa: usa los defaults"), de
        modo que el mismo descriptor valido en JSON se rechazaba en YAML.
        """
        if not isinstance(value, dict):
            return value
        return {key: ({} if sub_value is None else sub_value) for key, sub_value in value.items()}

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
DatasetOutput = Literal["logs", "csv", "dashboard", "raw", "files"]


# Variables globales del bloque `dataset`, declaradas por el modo de salida que
# las consume. Cada variable es exclusiva de sus modos: pedirla sin activar
# ninguno de ellos es un error de configuración, no un valor que se ignora en
# silencio (fail-fast). El modo 'logs' no admite variables.
DATASET_MODE_VARIABLES: dict[str, set[str]] = {
    "logs": set(),
    "csv": {"measurement", "influx_host", "influx_port", "influx_bucket"},
    "dashboard": {"measurement", "panel_interval"},
    "raw": {"measurement", "influx_bucket"},
}


class DatasetRequest(BaseModel):
    output: List[DatasetOutput] = Field(
        default_factory=lambda: ["logs"],
        description=(
            "Formato(s) de entrega del dataset. Acepta un único nombre ('logs') "
            "o una lista (['logs', 'csv']). Valores válidos: logs, csv, dashboard, raw."
        ),
    )

    # --- Variables globales por modo (ver DATASET_MODE_VARIABLES) ---
    # Todas son opcionales: si no se indican, el orquestador las deriva del
    # despliegue (IP de monitorización del report TNLCM, measurement del TestCase
    # de captura) y en último término usa el default del overlay.
    measurement: Optional[str] = Field(
        default=None,
        description=(
            "Measurement de InfluxDB del que se extrae el dataset. Modos: csv, "
            "dashboard, raw. Por defecto, el del TestCase de captura."
        ),
    )
    influx_host: Optional[str] = Field(
        default=None,
        description=(
            "IP/host de InfluxDB. Modo: csv. Por defecto, la IP de monitorización "
            "del report TNLCM."
        ),
    )
    influx_port: Optional[int] = Field(
        default=None, description="Puerto de InfluxDB. Modo: csv. Por defecto 8086."
    )
    influx_bucket: Optional[str] = Field(
        default=None,
        description="Bucket de InfluxDB. Modos: csv, raw. Por defecto 'testing'.",
    )
    panel_interval: Optional[str] = Field(
        default=None,
        description="Intervalo de refresco de los paneles Grafana. Modo: dashboard.",
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

    @model_validator(mode="after")
    def _reject_variables_of_inactive_modes(self) -> "DatasetRequest":
        """Rechazar variables cuyo modo dueño no está en `output` (fail-fast).

        Definir `influx_host` sin pedir 'csv' casi siempre significa que el modo
        se olvidó en `output`; aceptarlo en silencio produciría una entrega
        distinta de la esperada sin ningún aviso.
        """
        active = set(self.output)
        offending: dict[str, list[str]] = {}
        for variable in self.variables():
            owners = {mode for mode, names in DATASET_MODE_VARIABLES.items() if variable in names}
            if not owners & active:
                offending[variable] = sorted(owners)

        if offending:
            detail = "; ".join(
                f"'{name}' requiere dataset.output con alguno de {owners}"
                for name, owners in sorted(offending.items())
            )
            raise ValueError(f"dataset variables not applicable to the requested output: {detail}")
        return self

    def wants(self, kind: str) -> bool:
        """True si `kind` está entre los formatos de entrega solicitados."""
        return kind in self.output

    def variables(self) -> dict[str, Any]:
        """Variables globales realmente indicadas en el body (sin los None)."""
        known = {name for names in DATASET_MODE_VARIABLES.values() for name in names}
        return {
            name: getattr(self, name)
            for name in sorted(known)
            if getattr(self, name, None) is not None
        }


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


class DescriptorSource(BaseModel):
    """Procedencia del descriptor: en que formato llego y con que texto exacto.

    El descriptor puede entrar por tres vias (body JSON, body YAML o fichero YAML
    subido) y todas desembocan en el mismo `DatasetDescriptor`, asi que el modelo
    validado ya no dice como se escribio. Esa informacion hace falta mas abajo,
    en `storage`, para decidir que se persiste: el YAML siempre, y el JSON solo
    cuando el JSON fue lo que se envio.

    `raw` guarda el texto tal cual llego. Se conserva para poder persistirlo
    verbatim: una reserializacion perderia los comentarios, que son justamente lo
    que aporta YAML frente a JSON.
    """

    model_config = ConfigDict(frozen=True)

    format: Literal["json", "yaml"]
    raw: Optional[str] = Field(
        default=None,
        description="Texto original del descriptor; None cuando llego como JSON ya parseado",
    )

    @property
    def is_yaml(self) -> bool:
        return self.format == "yaml"
