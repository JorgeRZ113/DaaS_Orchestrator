"""El catalogo de componentes no puede desviarse de los overlays.

`examples/descriptors/REFERENCIA_componentes.yaml` enumera que se puede tocar de
cada componente. No es documentacion escrita a mano: se genera de
`templates/TNLCM/overlays/*.overlay.yaml`, que es la unica fuente de verdad,
porque el esquema de `infrastructure.component` no es estatico y lo declaran los
overlays en tiempo de ejecucion.

Sin esta prueba el catalogo envejece en silencio: alguien anade un campo a un
overlay, el usuario no se entera de que existe, y quien lea el catalogo creera
que la lista esta completa.
"""

import os
from pathlib import Path

import pytest
import yaml

from app.rendering.overlays import overlay_editable_fields_for_template
from app.rendering.paths import resolve_template_path
from app.rendering.tnlcm.overlay import COMPONENT_PARAMETER_MAPPING

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "examples" / "descriptors" / "REFERENCIA_componentes.yaml"
TEMPLATES_DIR = ROOT / "templates" / "TNLCM" / "templates"


def _component_names() -> list[str]:
    names = []
    for filename in sorted(os.listdir(TEMPLATES_DIR)):
        if filename == "base_tnlcm_descriptor.yaml":
            names.append("base")
        else:
            names.append(filename.replace("_sample_tnlcm_descriptor.yaml", ""))
    return names


def _editable_fields(component: str) -> dict[str, set[str]]:
    candidate = (
        "base_tnlcm_descriptor.yaml"
        if component == "base"
        else f"{component}_sample_tnlcm_descriptor.yaml"
    )
    path = resolve_template_path(candidate, category="TNLCM")
    assert path is not None, f"sin plantilla para {component}"
    return overlay_editable_fields_for_template(str(path), category="TNLCM")


def _reference() -> dict:
    return yaml.safe_load(REFERENCE.read_text(encoding="utf-8"))


COMPONENTS = _component_names()


def test_reference_file_exists() -> None:
    assert REFERENCE.exists()


def test_every_component_is_catalogued() -> None:
    """Una plantilla nueva sin entrada en el catalogo pasa desapercibida."""
    assert set(_reference()) == set(COMPONENTS)


@pytest.mark.parametrize("component", COMPONENTS)
def test_catalogued_fields_match_the_overlay(component: str) -> None:
    documented = {
        section: set(fields) for section, fields in (_reference()[component] or {}).items()
    }

    assert documented == {
        section: set(fields) for section, fields in _editable_fields(component).items()
    }


@pytest.mark.parametrize(
    "component", [c for c in COMPONENTS if COMPONENT_PARAMETER_MAPPING.get(c, {}).get("required")]
)
def test_required_fields_are_marked(component: str) -> None:
    """Los obligatorios llevan [OBLIGATORIO] al lado; es lo que se busca al leerlo."""
    lines = REFERENCE.read_text(encoding="utf-8").splitlines()

    for field in COMPONENT_PARAMETER_MAPPING[component]["required"]:
        marked = [ln for ln in lines if ln.strip().startswith(f"{field}:") and "OBLIGATORIO" in ln]
        assert marked, f"{component}.{field} es obligatorio y no esta marcado"


def test_ambiguous_components_are_flagged() -> None:
    """Donde un campo vive en dos secciones, el formato plano pierde uno.

    Hoy le pasa a int_p4_sw (`name`) y a ocf (`vnet_*`). Quien copie del catalogo
    en formato plano tiene que saberlo antes, no al ver el despliegue mal.
    """
    text = REFERENCE.read_text(encoding="utf-8")

    for component in COMPONENTS:
        seen: dict[str, int] = {}
        for fields in _editable_fields(component).values():
            for field in fields:
                seen[field] = seen.get(field, 0) + 1
        if any(count > 1 for count in seen.values()):
            header = next(ln for ln in text.splitlines() if ln.startswith(f"# {component}  --"))
            assert "AMBIGUO EN PLANO" in header, f"{component} tiene campos repetidos sin avisar"


def test_reference_is_not_a_descriptor() -> None:
    """Es un catalogo: no debe colarse como ejemplo enviable."""
    assert not REFERENCE.name[0].isdigit()
    assert "infrastructure" not in _reference()
