"""Normalizacion de `component.<template>.<campo>` contra los campos editables.

El extractor acepta dos formatos —plano (canonico) y anidado por seccion
(retrocompatibilidad)— y devuelve, ademas de lo extraido, la lista de rutas
invalidas para que el endpoint pueda responder con TODAS de una vez en lugar de
fallar en la primera.

Hasta ahora esta logica solo se ejercitaba de rebote a traves del endpoint, pese
a ser una funcion pura sin I/O.
"""

from app.domain.component_contract import extract_component_template_values

# Lo que declararia el overlay de `base`: dos secciones con sus campos editables.
EDITABLE = {
    "monitoring": {"influxdb_user", "influxdb_password"},
    "elcm": {"elcm_version"},
}


def test_flat_format_groups_fields_under_their_section():
    extracted, invalids = extract_component_template_values(
        "base", {"influxdb_user": "admin", "elcm_version": "1.2"}, EDITABLE
    )

    assert extracted == {"monitoring": {"influxdb_user": "admin"}, "elcm": {"elcm_version": "1.2"}}
    assert invalids == []


def test_nested_format_is_accepted_for_backwards_compatibility():
    extracted, invalids = extract_component_template_values(
        "base", {"monitoring": {"influxdb_user": "admin"}}, EDITABLE
    )

    assert extracted == {"monitoring": {"influxdb_user": "admin"}}
    assert invalids == []


def test_both_formats_merge_into_the_same_section():
    extracted, invalids = extract_component_template_values(
        "base",
        {"monitoring": {"influxdb_user": "admin"}, "influxdb_password": "secret"},
        EDITABLE,
    )

    assert extracted["monitoring"] == {"influxdb_user": "admin", "influxdb_password": "secret"}
    assert invalids == []


def test_unknown_flat_field_is_reported_not_silently_dropped():
    extracted, invalids = extract_component_template_values("base", {"no_existe": "x"}, EDITABLE)

    assert extracted == {}
    assert invalids == ["component.base.no_existe: field not allowed"]


def test_unknown_section_is_reported():
    _, invalids = extract_component_template_values("base", {"inventada": {"a": 1}}, EDITABLE)

    assert invalids == ["component.base.inventada: section not allowed"]


def test_unknown_field_inside_a_known_section_is_reported():
    extracted, invalids = extract_component_template_values(
        "base", {"monitoring": {"influxdb_user": "admin", "colado": 1}}, EDITABLE
    )

    # Lo valido se conserva; solo se rechaza el campo que sobra.
    assert extracted == {"monitoring": {"influxdb_user": "admin"}}
    assert invalids == ["component.base.monitoring.colado: field not allowed"]


def test_all_invalid_paths_are_collected_in_one_pass():
    """El endpoint responde con la lista completa, no falla en el primero."""
    _, invalids = extract_component_template_values(
        "base", {"malo_1": 1, "malo_2": 2, "inventada": {"x": 1}}, EDITABLE
    )

    assert len(invalids) == 3


def test_name_that_is_both_section_and_field_is_rejected_as_ambiguous():
    # Si un nombre vale como seccion y como campo, el formato plano no puede
    # resolverlo sin adivinar: se exige el anidado.
    editable = {"monitoring": {"monitoring"}}

    _, invalids = extract_component_template_values("base", {"monitoring": "x"}, editable)

    assert invalids == [
        "component.base.monitoring: ambiguous (is both section and field), use nested format"
    ]


def test_empty_payload_produces_nothing():
    assert extract_component_template_values("base", {}, EDITABLE) == ({}, [])
