"""Validaciones que se adelantan al endpoint para no fallar a mitad del despliegue.

Todo lo que se puede comprobar sin tocar la infraestructura se comprueba aqui y
se responde con un 400 que enumera **todos** los problemas de una vez. La
alternativa —dejarlo para la fase, que corre en segundo plano— significa
descubrir un nombre mal escrito cuando la TN ya esta levantada, con un
experimento FAILED y ese nombre quemado para el resto de la vida de la TN.
"""

from typing import Any

from fastapi import HTTPException

from app.domain.component_contract import extract_component_template_values
from app.domain.descriptor import DatasetRequest, ExperimentConfig, InfrastructureConfig
from app.rendering.overlays import overlay_editable_fields_for_template
from app.rendering.paths import resolve_template_path
from app.rendering.tnlcm.overlay import COMPONENT_PARAMETER_MAPPING
from app.services import preflight


def collect_empty_string_paths(value: Any, prefix: str = "") -> list[str]:
    """Rutas de todos los strings vacios del body, en formato dot-path.

    Un valor "" (o solo espacios) casi siempre es un campo que el cliente dejo a
    medias: o lo rellena con un valor real o lo elimina del body. Se devuelven
    rutas tipo "infrastructure.component.base.grafana_password" para que sepa
    exactamente que corregir antes de reenviar.
    """
    empty_paths: list[str] = []

    if isinstance(value, str):
        if value.strip() == "":
            empty_paths.append(prefix or "<root>")
    elif isinstance(value, dict):
        for key, sub_value in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            empty_paths.extend(collect_empty_string_paths(sub_value, child_prefix))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            empty_paths.extend(collect_empty_string_paths(item, f"{prefix}[{index}]"))

    return empty_paths


def reject_empty_strings_or_raise(descriptor) -> None:
    """Fail-fast: si algo llego vacio no se ejecuta nada.

    Solo se inspecciona lo que el cliente envio (`exclude_unset`) para no marcar
    los valores por defecto del servidor.
    """
    empty_fields = collect_empty_string_paths(descriptor.model_dump(exclude_unset=True))
    if not empty_fields:
        return

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


def validate_components_or_raise(infra: InfrastructureConfig) -> None:
    """Comprueba `infrastructure.component` contra los overlays reales.

    El esquema no es estatico: lo declaran los `.overlay.yaml` en tiempo de
    ejecucion, asi que hay que resolver la plantilla de cada componente para
    saber que campos admite.
    """
    comps = infra.component or {}
    invalids: list[str] = []

    for comp_key, comp_values in comps.items() if isinstance(comps, dict) else []:
        if not isinstance(comp_values, dict):
            # Un valor no-dict es aceptable: significa "usa los defaults".
            comp_values = {}

        candidate = (
            "base_tnlcm_descriptor.yaml"
            if comp_key == "base"
            else f"{comp_key}_sample_tnlcm_descriptor.yaml"
        )
        comp_template = resolve_template_path(candidate, category="TNLCM")

        if comp_template is None:
            invalids.append(f"component.{comp_key}: template not found")
            continue

        allowed = overlay_editable_fields_for_template(str(comp_template), category="TNLCM")
        editable_by_section: dict[str, set[str]] = {
            section: set(fields) for section, fields in allowed.items()
        }

        extracted, component_invalids = extract_component_template_values(
            comp_key=comp_key,
            comp_values=comp_values,
            editable_by_section=editable_by_section,
        )
        invalids.extend(component_invalids)

        # Campos obligatorios: se comprueban aqui para cortar con 400 en vez de
        # dejar que la ejecucion falle en segundo plano al rellenar el overlay.
        required = set(COMPONENT_PARAMETER_MAPPING.get(comp_key, {}).get("required", []))
        provided = {field for fields in extracted.values() for field in fields}
        for field in sorted(required - provided):
            invalids.append(f"component.{comp_key}.{field}: required field missing")

    if invalids:
        raise HTTPException(status_code=400, detail={"invalid_fields": invalids})


def validate_elcm_or_raise(experiment: ExperimentConfig, dataset: DatasetRequest) -> None:
    """Traduce el pre-flight ELCM (`app.services.preflight`) a un 400 accionable."""
    try:
        preflight.validate_elcm_request(experiment, dataset)
    except preflight.ElcmPreflightError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "invalid_experiment": exc.problems,
                "message": (
                    "La parte ELCM de la peticion no se puede ejecutar tal cual. "
                    "Corrige lo indicado y reenvia la peticion; no se ha desplegado "
                    "ni modificado nada."
                ),
            },
        ) from exc
