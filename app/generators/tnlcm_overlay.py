"""
TNLCM Overlay Processing: Fase 1 - Fill empty overlay parameters with DataDescriptor values.

Responsabilidades:
- Leer overlay templates desde templates/TNLCM/overlays/{comp}.overlay.yaml
- Identificar campos vacíos ("") en el overlay
- Obtener valores correspondientes del DataDescriptor (infrastructure.component.<comp>)
- Guardar overlay relleno con cabecera #@data/values en artifacts/<execution_id>/archivos_generados/

Sistema de Mapeo Dinámico:
- COMPONENT_PARAMETER_MAPPING define parámetros requeridos y opcionales por componente
- Validación estricta: rechaza campos no declarados en el mapeo
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml

from app.utils.custom_yaml import _wrap_strings_in_quotes

logger = logging.getLogger(__name__)


# ============================================================================
# MAPEO DINÁMICO DE PARÁMETROS POR COMPONENTE
# ============================================================================
# Estructura: {component_name: {"required": [...], "optional": [...]}}
# Cada sección define qué parámetros espera del DataDescriptor
# Los parámetros opcionales se ignoran si no vienen; si vienen, se inyectan
COMPONENT_PARAMETER_MAPPING: dict[str, dict[str, list[str]]] = {
    "base": {
        "required": ["influxdb_user", "influxdb_password", "grafana_password"], # O ponlos en required si son obligatorios
        "optional": ["influxdb_org", "influxdb_bucket", "influxdb_token" ],
    },
    "mongodb": {
        "required": [],
        "optional": ["database", "replica_set"],
    },
    "redis": {
        "required": [],
        "optional": ["cache", "persistence"],
    },
    "vnet": {
        "required": [],
        "optional": ["network", "subnets"],
    },
    "vm_kvm": {
        "required": [],
        "optional": ["vm", "compute"],
    },
    "open5gs_vm": {
        "required": [],
        "optional": ["open5gs", "network"],
    },
    "ueransim": {
        "required": [],
        "optional": ["gnb_linked_5gcore"],
    },
    "ueransim_both": {
        "required": [],
        "optional": ["gnb_linked_5gcore"],
    },
    "ueransim_split": {
        "required": [],
        "optional": ["ueransim", "network"],
    },
    "upf_p4_sw": {
        "required": [],
        "optional": ["upf", "network"],
    },
    "int_p4_sw": {
        "required": [],
        "optional": ["int", "network"],
    },
    "loadcore_agent": {
        "required": [],
        "optional": ["loadcore", "network"],
    },
    "oneKE": {
        "required": [],
        "optional": ["oneKE", "network"],
    },
    "ocf": {
        "required": [],
        "optional": ["ocf", "network"],
    },
}


class InvalidDataDescriptorError(ValueError):
    """Excepción lanzada cuando el DataDescriptor contiene campos no permitidos en overlays."""

    def __init__(self, invalid_fields: list[str]):
        self.invalid_fields = invalid_fields
        super().__init__(f"Invalid fields in DataDescriptor: {', '.join(invalid_fields)}")


class MissingComponentParameterError(ValueError):
    """Excepción lanzada cuando faltan parámetros requeridos para un componente."""

    def __init__(self, component: str, missing_params: list[str]):
        self.component = component
        self.missing_params = missing_params
        super().__init__(
            f"Component '{component}' missing required parameters: {', '.join(missing_params)}"
        )


def _timer(execution_id: str, phase_name: str = "overlay"):
    """Contexto de timing para logging."""
    class Timer:
        def __init__(self, exec_id: str, phase: str):
            self.exec_id = exec_id
            self.phase = phase
            self.start_time = time.time()

        def stop(self, status: str = "success") -> None:
            elapsed = time.time() - self.start_time
            logger.info(f"[{self.exec_id}] {self.phase} completed in {elapsed:.2f}s (status={status})")

    return Timer(execution_id, phase_name)


def _ensure_generated_dir(execution_id: str) -> Path:
    """Crear y retornar el directorio artifacts/<execution_id>/archivos_generados/."""
    generated_dir = Path("artifacts") / execution_id / "archivos_generados"
    generated_dir.mkdir(parents=True, exist_ok=True)
    return generated_dir


def _save_text(file_path: Path, content: str) -> None:
    """Guardar contenido a archivo."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    logger.debug(f"Saved: {file_path}")


def _load_overlay_yaml(overlay_path: Path) -> dict[str, Any]:
    """Cargar overlay YAML, ignorando la cabecera #@data/values."""
    content = overlay_path.read_text(encoding="utf-8")

    # Saltar la cabecera @data/values y separador ---
    lines = content.split("\n")
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("#@data/values") or line.strip().startswith("@data/values"):
            # Encontrar la línea '---' después de la cabecera
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == "---":
                    body_start = j + 1
                    break
            break

    body = "\n".join(lines[body_start:])
    parsed = yaml.safe_load(body)
    return parsed if isinstance(parsed, dict) else {}


def _fill_empty_values(
    overlay_dict: dict[str, Any],
    flat_extracted_values: dict[str, Any],
) -> dict[str, Any]:
    """
    Sobrescribir campos del overlay con valores del diccionario plano cuando el
    usuario los proporciona; conservar el valor del overlay (vacío o default) en
    caso contrario.

    Itera sobre las secciones del overlay. Para cada campo, si su nombre aparece
    en flat_extracted_values (porque el usuario lo envió en la request), se usa
    ese valor; si no, se mantiene el valor que ya trae el overlay, sea "" (campo
    obligatorio sin default) o un valor real (campo opcional con default).

    Args:
        overlay_dict: Estructura del overlay {section: {field: value, ...}}
        flat_extracted_values: Diccionario plano {field_name: value, ...}

    Returns:
        Overlay relleno con estructura {section: {field: value, ...}}
    """
    filled = {}

    for section_name, section_data in overlay_dict.items():
        if not isinstance(section_data, dict):
            filled[section_name] = section_data
            continue

        filled[section_name] = {}

        for field_name, field_value in section_data.items():
            if field_name in flat_extracted_values:
                filled[section_name][field_name] = flat_extracted_values[field_name]
            else:
                filled[section_name][field_name] = field_value

    return filled


def _validate_component_allowed(comp_key: str) -> None:
    """
    Validar que el componente esté declarado en el mapeo dinámico.

    Lanza ValueError si el componente no está en COMPONENT_PARAMETER_MAPPING.

    Args:
        comp_key: Nombre del componente

    Raises:
        ValueError: Si el componente no está permitido
    """
    if comp_key not in COMPONENT_PARAMETER_MAPPING:
        raise ValueError(
            f"Component '{comp_key}' is not declared in COMPONENT_PARAMETER_MAPPING. "
            f"Allowed components: {', '.join(sorted(COMPONENT_PARAMETER_MAPPING.keys()))}"
        )


def _extract_component_values_from_mapping(
    comp_key: str,
    comp_values: dict[str, Any],
) -> dict[str, Any]:
    """
    Extraer y validar valores del componente contra COMPONENT_PARAMETER_MAPPING.

    Soporta dos formatos de entrada:
    1. Plano: {field_name: value}
    2. Anidado: {section: {field_name: value}}

    Retorna un diccionario PLANO donde cada clave es un nombre de campo.
    Los valores anidados se aplanan automáticamente.

    Reglas:
    - TODOS los campos en "required" DEBEN estar presentes
    - Los campos en "optional" se inyectan si están presentes
    - Campos desconocidos (ni required ni optional) lanzan ValueError

    Args:
        comp_key: Nombre del componente
        comp_values: Diccionario con valores del usuario

    Returns:
        Dict plano {field_name: value, ...}

    Raises:
        MissingComponentParameterError: Si falta algún parámetro requerido
        ValueError: Si hay campos desconocidos no permitidos
    """
    # Obtener definición del componente
    component_def = COMPONENT_PARAMETER_MAPPING.get(comp_key, {})
    required_params = set(component_def.get("required", []))
    optional_params = set(component_def.get("optional", []))
    allowed_params = required_params | optional_params

    # Normalizar entrada
    if not isinstance(comp_values, dict):
        comp_values = {}

    # Recolectar parámetros presentes
    present_params: set[str] = set()
    extracted: dict[str, Any] = {}
    unknown_fields: list[str] = []

    for key, value in comp_values.items():
        # Caso 1: Valor es dict (formato anidado, sección-like)
        if isinstance(value, dict):
            section_name = key
            # La sección entera se trata como un parámetro permitido
            if section_name not in allowed_params:
                unknown_fields.append(section_name)
                continue
            present_params.add(section_name)
            # Aplastar: guardar cada subfield directamente en extracted
            for subfield_name, subfield_value in value.items():
                extracted[subfield_name] = subfield_value
            continue

        # Caso 2: Valor es escalar (formato plano, campo directo)
        field_name = key
        if field_name not in allowed_params:
            unknown_fields.append(field_name)
            continue

        present_params.add(field_name)
        extracted[field_name] = value

    # Validar campos desconocidos
    if unknown_fields:
        raise ValueError(
            f"Component '{comp_key}' has unknown/not-allowed fields: {', '.join(unknown_fields)}. "
            f"Allowed fields are: {', '.join(sorted(allowed_params))}"
        )

    # Validar que todos los required están presentes
    missing_required = required_params - present_params
    if missing_required:
        raise MissingComponentParameterError(
            component=comp_key,
            missing_params=sorted(missing_required),
        )

    return extracted


async def build_component_overlay_values(
    comp_key: str,
    comp_values: dict[str, Any],
    overlay_path: Path,
    execution_id: str,
) -> Path:
    """
    Fase 1: Rellenar overlay para un componente y guardar con cabecera @data/values.

    Flujo:
    1. Validar que el componente está en COMPONENT_PARAMETER_MAPPING
    2. Validar que se proporcionan TODOS los parámetros requeridos
    3. Leer overlay desde templates/TNLCM/overlays/{comp}.overlay.yaml
    4. Identificar campos "" (vacíos)
    5. Inyectar valores permitidos (required + optional presentes)
    6. Guardar overlay relleno con cabecera #@data/values

    Args:
        comp_key: Nombre del componente (ej. "base", "mongodb")
        comp_values: Valores del DataDescriptor para este componente
        overlay_path: Ruta al overlay template
        execution_id: ID de la ejecución

    Returns:
        Path al archivo overlay rellenado

    Raises:
        ValueError: Si el componente no está permitido o hay campos desconocidos
        MissingComponentParameterError: Si faltan parámetros requeridos
        FileNotFoundError: Si el overlay no existe
    """
    timer = _timer(execution_id, f"overlay_filling[{comp_key}]")

    # Validación 1: Componente debe estar en mapeo dinámico
    _validate_component_allowed(comp_key)

    if not overlay_path.exists():
        raise FileNotFoundError(f"Overlay not found: {overlay_path}")

    # Cargar overlay
    overlay_dict = _load_overlay_yaml(overlay_path)
    logger.debug(f"[{execution_id}] Loaded overlay for {comp_key}: {overlay_path}")

    # Validación 2 y 3: Extraer y validar valores contra el mapeo dinámico
    # Esto valida required, optional, y rechaza desconocidos
    extracted = _extract_component_values_from_mapping(comp_key, comp_values)
    logger.debug(f"[{execution_id}] Extracted component values for {comp_key}: {extracted}")

    # Rellenar overlay con valores extraídos
    filled_overlay = _fill_empty_values(overlay_dict, extracted)

    # Guardar con cabecera @data/values obligatoria
    generated_dir = _ensure_generated_dir(execution_id)
    output_path = generated_dir / f"{comp_key}_overlay_filled.yaml"

    # Serializar a YAML con cabecera
    # Forzar comillas dobles para que el sistema objetivo (ELCM) reciba sfilled_with_header = f"#@data/values\n---\n{yaml_content}"trings entrecomillados
    # Envolver solo los valores string y quitar default_style
    quoted_overlay = _wrap_strings_in_quotes(filled_overlay)
    yaml_content = yaml.safe_dump(quoted_overlay, sort_keys=False, allow_unicode=True)
    # Usar cabecera estándar con espacio: '#@ data/values' para cumplir sintaxis YTT
    filled_with_header = f"#@data/values\n---\n{yaml_content}"

    _save_text(output_path, filled_with_header)
    logger.info(f"[{execution_id}] Filled overlay saved: {output_path}")

    timer.stop(status="success")
    return output_path




