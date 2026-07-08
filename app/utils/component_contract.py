"""
Extractor centralizado para normalizar y validar campos component.<template>.<field>
contra campos editables del overlay TNLCM.

Soporta dos formatos de payload:
1. Plano (canónico): component.base.influxdb_user = "admin"
2. Anidado (retrocompatibilidad): component.base.monitoring.influxdb_user = "admin"

El extractor mapea ambos formatos a un diccionario normalizado:
{
    "monitoring": {"influxdb_user": "admin", ...},
    ...
}
"""

from typing import Any


def extract_component_template_values(
    comp_key: str,
    comp_values: dict[str, Any],
    editable_by_section: dict[str, set[str]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """
    Normaliza y valida campos component.<template> contra editables del overlay.

    Acepta:
    - Formato plano: component.<template>.<field> = value
    - Formato anidado: component.<template>.<section>.<field> = value

    Args:
        comp_key: Nombre del template (ej. 'base', 'mongodb', 'redis')
        comp_values: Diccionario con valores a extraer
        editable_by_section: Dict { section: set(field_names) } de campos permitidos

    Returns:
        (extracted, invalids) donde:
        - extracted: Dict { section: { field: value } } con valores normalizados
        - invalids: List[str] con rutas de campos inválidos
    """
    extracted: dict[str, dict[str, Any]] = {}
    invalids: list[str] = []

    # Mapeo field -> section para fácil lookup en formato plano
    field_to_section: dict[str, str] = {}
    for section, fields in editable_by_section.items():
        for field in fields:
            field_to_section[field] = section

    editable_fields = set(field_to_section.keys())

    for key, value in comp_values.items():
        # Retrocompatibilidad: valor es dict (format anidado section-like)
        if isinstance(value, dict):
            section = key
            if section not in editable_by_section:
                invalids.append(
                    f"component.{comp_key}.{section}: section not allowed"
                )
                continue
            # Validar cada campo dentro de la sección
            for field_name, field_value in value.items():
                if field_name not in editable_by_section[section]:
                    invalids.append(
                        f"component.{comp_key}.{section}.{field_name}: field not allowed"
                    )
                else:
                    extracted.setdefault(section, {})[field_name] = field_value
            continue

        # Formato canónico plano: component.<template>.<field>
        field_name = key
        
        # Verificar si el nombre es una sección permitida (caso especial de ambigüedad)
        if field_name in editable_by_section:
            invalids.append(
                f"component.{comp_key}.{field_name}: ambiguous (is both section and field), use nested format"
            )
            continue

        # Validar que el campo esté permitido
        if field_name not in editable_fields:
            invalids.append(
                f"component.{comp_key}.{field_name}: field not allowed"
            )
            continue

        # Mapear el campo a su sección y añadir al resultado
        section = field_to_section[field_name]
        extracted.setdefault(section, {})[field_name] = value

    return extracted, invalids

