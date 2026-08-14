"""
TNLCM Overlay Processing: Fase 1 - Fill empty overlay parameters with DatasetDescriptor values.

Responsabilidades:
- Obtener valores correspondientes del DatasetDescriptor (infrastructure.component.<comp>)
- COMPONENT_PARAMETER_MAPPING solo declara qué campos son OBLIGATORIOS; el resto
    que aparezca en el overlay es opcional y viaja con su valor por defecto.
- Validación estricta: rechaza campos que no estén declarados en el overlay.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml

from app.rendering.yaml_style import _wrap_strings_in_quotes

logger = logging.getLogger(__name__)


# ============================================================================
# Solo se declaran aquí los campos OBLIGATORIOS. El conjunto de campos permitidos
# (y por tanto los opcionales) se deriva del propio overlay en cada ejecución, de
# modo que overlay y validación no puedan desincronizarse: añadir un campo al
# overlay basta para poder enviarlo en el DatasetDescriptor.
#
# Un componente solo puede desplegarse si aparece en este diccionario Y tiene su
# par template/overlay en templates/TNLCM/.
COMPONENT_PARAMETER_MAPPING: dict[str, dict[str, list[str]]] = {
    "base": {
        "required": [
            "influxdb_user",
            "influxdb_password",
            "grafana_password",
        ],
    },
    "tn_init": {"required": []},
    "vnet": {"required": []},
    "vm_kvm": {"required": []},
    "mongodb": {
        "required": [
            "user",
            "password",
            "database",
            "express_user",
            "express_password",
        ],
    },
    "open5gs_vm": {"required": []},
    "ueransim_both": {"required": []},
    "ueransim_split": {"required": []},
    "int_p4_sw": {"required": []},
    "oneKE": {"required": []},
    "ocf": {"required": []},
}


class InvalidDatasetDescriptorError(ValueError):
    """Excepción lanzada cuando el DatasetDescriptor contiene campos no permitidos en overlays."""

    def __init__(self, invalid_fields: list[str]):
        self.invalid_fields = invalid_fields
        super().__init__(f"Invalid fields in DatasetDescriptor: {', '.join(invalid_fields)}")


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
            logger.info(
                f"[{self.exec_id}] {self.phase} completed in {elapsed:.2f}s (status={status})"
            )

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
    """
    if comp_key not in COMPONENT_PARAMETER_MAPPING:
        raise ValueError(
            f"Component '{comp_key}' is not declared in COMPONENT_PARAMETER_MAPPING. "
            f"Allowed components: {', '.join(sorted(COMPONENT_PARAMETER_MAPPING.keys()))}"
        )


def _overlay_allowed_names(overlay_dict: dict[str, Any]) -> tuple[set[str], set[str]]:
    """
    Derivar del overlay los nombres aceptados en el DatasetDescriptor.
    """
    sections: set[str] = set()
    fields: set[str] = set()

    for section_name, section_data in overlay_dict.items():
        if not isinstance(section_data, dict):
            continue
        sections.add(section_name)
        fields.update(section_data.keys())

    return sections, fields


def _extract_component_values_from_mapping(
    comp_key: str,
    comp_values: dict[str, Any],
    overlay_dict: dict[str, Any],
) -> dict[str, Any]:
    """
    Extraer y validar valores del componente contra su overlay.

    Formatos de entrada:
    1. Plano: {field_name: value}
    2. Anidado: {section: {field_name: value}}

    Reglas:
    - Los nombres permitidos son las secciones y los campos declarados en el overlay
    - TODOS los campos de COMPONENT_PARAMETER_MAPPING[comp]["required"] DEBEN estar
      presentes, se envíen en formato plano o anidado
    - El resto de campos del overlay son opcionales: si no vienen, se conserva su
      valor por defecto
    """
    component_def = COMPONENT_PARAMETER_MAPPING.get(comp_key, {})
    required_params = set(component_def.get("required", []))

    allowed_sections, allowed_fields = _overlay_allowed_names(overlay_dict)
    allowed_params = allowed_sections | allowed_fields

    # Fail-fast ante desincronización: un required que el overlay no declara nunca
    # podría satisfacerse y dejaría el componente permanentemente inservible.
    undeclared_required = required_params - allowed_fields
    if undeclared_required:
        raise ValueError(
            f"Component '{comp_key}' declares required fields that its overlay does not "
            f"define: {', '.join(sorted(undeclared_required))}"
        )

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
            if section_name not in allowed_sections:
                unknown_fields.append(section_name)
                continue
            present_params.add(section_name)
            # Aplastar: guardar cada subfield directamente en extracted
            for subfield_name, subfield_value in value.items():
                if subfield_name not in allowed_fields:
                    unknown_fields.append(f"{section_name}.{subfield_name}")
                    continue
                present_params.add(subfield_name)
                extracted[subfield_name] = subfield_value
            continue

        # Caso 2: Valor es escalar o lista (formato plano, campo directo)
        field_name = key
        if field_name not in allowed_fields:
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
    Fase 1: Rellenar overlay para un componente

    Flujo:
    1. Validar que el componente está en COMPONENT_PARAMETER_MAPPING
    2. Leer overlay desde templates/TNLCM/overlays/{comp}.overlay.yaml
    3. Derivar del overlay los campos permitidos y validar contra ellos, exigiendo
       los declarados como requeridos y rechazando los desconocidos
    4. Sobrescribir con los valores del usuario; los campos que no vengan conservan
       su valor por defecto del overlay
    5. Guardar overlay relleno
    """
    timer = _timer(execution_id, f"overlay_filling[{comp_key}]")

    # Validación 1: Componente debe estar en mapeo dinámico
    _validate_component_allowed(comp_key)

    if not overlay_path.exists():
        raise FileNotFoundError(f"Overlay not found: {overlay_path}")

    # Cargar overlay
    overlay_dict = _load_overlay_yaml(overlay_path)
    logger.debug(f"[{execution_id}] Loaded overlay for {comp_key}: {overlay_path}")

    # Validación 2 y 3: Extraer y validar valores contra el propio overlay
    # Esto valida required, opcionales, y rechaza desconocidos
    extracted = _extract_component_values_from_mapping(comp_key, comp_values, overlay_dict)
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
