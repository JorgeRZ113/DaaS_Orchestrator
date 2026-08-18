"""El descriptor en YAML y en JSON debe producir exactamente la misma estructura.

Esa equivalencia es lo que permite afirmar que el formato de transporte es
indiferente, y no sale gratis: PyYAML implementa YAML 1.1, que resuelve tipos de
forma distinta a JSON. Estas pruebas fijan la diferencia para que un cambio en el
loader no la reintroduzca en silencio.
"""

import json

import pytest
import yaml

from app.api.body_formats import JsonLikeLoader, parse_yaml_descriptor


def _load(text: str):
    return yaml.load(text, Loader=JsonLikeLoader)


# (documento, valor esperado) para los casos donde YAML 1.1 se desvia de JSON.
YAML_11_TRAPS = [
    ("v: no", "no"),
    ("v: off", "off"),
    ("v: yes", "yes"),
    ("v: on", "on"),
    ("v: Off", "Off"),
    ("v: 22:30", "22:30"),  # sexagesimal: safe_load devolveria 1350
    ("v: 007", "007"),  # safe_load devolveria 7 y perderia los ceros
]


@pytest.mark.parametrize("document,expected", YAML_11_TRAPS)
def test_yaml_11_type_coercion_is_disabled(document: str, expected: str) -> None:
    """Los valores que YAML 1.1 convertiria a bool o a int siguen siendo texto."""
    assert _load(document)["v"] == expected


@pytest.mark.parametrize("document,expected", YAML_11_TRAPS)
def test_safe_load_would_have_coerced_them(document: str, expected: str) -> None:
    """Contraprueba: sin el loader propio, estos casos SI cambian de tipo.

    Documenta por que existe `JsonLikeLoader`; si PyYAML cambiara su
    comportamiento por defecto, esta prueba avisaria de que ya no hace falta.
    """
    assert yaml.safe_load(document)["v"] != expected


@pytest.mark.parametrize(
    "document,expected",
    [
        ("v: true", True),
        ("v: false", False),
        ("v: null", None),
        ("v: 3", 3),
        ("v: -2.5", -2.5),
        ("v: hola", "hola"),
        ("v: 1.0.0", "1.0.0"),
    ],
)
def test_core_schema_types_are_preserved(document: str, expected) -> None:
    """Lo que JSON tipa igual debe seguir tipandose igual."""
    value = _load(document)["v"]
    assert value == expected
    assert type(value) is type(expected)


def test_yaml_and_json_produce_the_same_structure() -> None:
    """La propiedad que sostiene todo: mismas claves, mismos tipos, mismos valores."""
    descriptor_yaml = """
    infrastructure:
      name: tn_demo
      component:
        base:
          influxdb_user: admin
          deploy_extra: no
          port_window: 22:30
      parameters:
        library_reference_type: branch
        library_reference_value: develop
    experiment:
      name: demo
      testcase_paths:
        - TC_ping.yml
      ues_paths: []
    dataset:
      output: [logs]
    auto_start_elcm: true
    ephemeral_tn: false
    """

    descriptor_json = json.dumps(
        {
            "infrastructure": {
                "name": "tn_demo",
                "component": {
                    "base": {
                        "influxdb_user": "admin",
                        "deploy_extra": "no",
                        "port_window": "22:30",
                    }
                },
                "parameters": {
                    "library_reference_type": "branch",
                    "library_reference_value": "develop",
                },
            },
            "experiment": {"name": "demo", "testcase_paths": ["TC_ping.yml"], "ues_paths": []},
            "dataset": {"output": ["logs"]},
            "auto_start_elcm": True,
            "ephemeral_tn": False,
        }
    )

    assert _load(descriptor_yaml) == json.loads(descriptor_json)


def test_anchors_and_aliases_still_work() -> None:
    """Reconstruir la tabla de resolvers no debe romper el resto de YAML."""
    document = """
    defaults: &defaults
      influxdb_user: admin
    infrastructure:
      component:
        base: *defaults
    """
    assert _load(document)["infrastructure"]["component"]["base"] == {"influxdb_user": "admin"}


# --- parse_yaml_descriptor: traduccion de fallos a HTTP ---


def _status_and_detail(document: str) -> tuple[int, dict]:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        parse_yaml_descriptor(document)
    return excinfo.value.status_code, excinfo.value.detail


def test_malformed_yaml_reports_line_and_column() -> None:
    status, detail = _status_and_detail("infrastructure:\n  name: tn\n   bad: indent\n")
    assert status == 400
    assert detail["line"] >= 1
    assert detail["column"] >= 1
    assert "yaml_error" in detail


def test_empty_document_is_rejected() -> None:
    status, detail = _status_and_detail("")
    assert status == 400
    assert detail["yaml_error"] == "empty document"


def test_non_mapping_root_is_rejected() -> None:
    status, detail = _status_and_detail("- uno\n- dos\n")
    assert status == 400
    assert "list" in detail["yaml_error"]


def test_multiple_documents_are_rejected() -> None:
    status, detail = _status_and_detail("infrastructure:\n  name: a\n---\nother: b\n")
    assert status == 400
    assert "documento" in detail["message"]


def test_valid_mapping_is_returned_untouched() -> None:
    assert parse_yaml_descriptor("infrastructure:\n  name: tn_demo\n") == {
        "infrastructure": {"name": "tn_demo"}
    }


# --- escalar vacio: `clave:` sin nada detras ---


@pytest.mark.parametrize("document", ["v:", "v: null", "v: ~", "v: NULL"])
def test_empty_and_explicit_nulls_resolve_to_none(document: str) -> None:
    """`componente:` a secas tiene que ser null, no cadena vacia.

    PyYAML resuelve el escalar vacio con la clave "" en la tabla de resolvers, no
    con el primer caracter. Si se omite, `ueransim_both:` llega como "" y
    `reject_empty_strings_or_raise` lo rechaza con un 400 de campos vacios en vez
    de entenderse como «despliegalo con sus defaults».
    """
    assert _load(document)["v"] is None


def test_empty_scalar_matches_pyyaml_default() -> None:
    """En esto no se busca desviarse de PyYAML, solo en el esquema de tipos."""
    assert _load("v:")["v"] == yaml.safe_load("v:")["v"]


def test_quoted_empty_string_is_still_a_string() -> None:
    """Un `v: ''` explicito es una cadena vacia, y debe seguir siendolo."""
    assert _load("v: ''")["v"] == ""


def test_component_left_empty_parses_as_none() -> None:
    """El caso real que motiva lo anterior, tal cual se escribe en el descriptor."""
    parsed = parse_yaml_descriptor(
        "infrastructure:\n  name: tn\n  component:\n    base:\n    ueransim_both:\n"
    )

    assert parsed["infrastructure"]["component"] == {"base": None, "ueransim_both": None}
