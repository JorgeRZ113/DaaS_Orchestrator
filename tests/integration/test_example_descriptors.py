"""Los descriptores de ejemplo tienen que seguir siendo validos.

`examples/descriptors/` es material de documentacion y del manual de usuario: un
ejemplo que ya no valida es peor que no tener ejemplo, porque el usuario lo copia
y descubre el fallo cuando la peticion le rebota.

Se valida con la misma cadena que atraviesa una peticion real -- modelo, campos
vacios, componentes contra los overlays y pre-flight de ELCM -- porque los fallos
que de verdad aparecen no son de esquema: un nombre de componente inexistente o
un modo de dataset sin su TestCase de captura pasan el modelo y revientan
despues.
"""

from pathlib import Path

import pytest
import yaml

from app.api.body_formats import JsonLikeLoader
from app.api.schemas.requests import ElcmExperimentRequest
from app.api.validation import reject_empty_strings_or_raise, validate_components_or_raise
from app.domain.descriptor import DatasetDescriptor
from app.services import preflight

DESCRIPTORS_DIR = Path(__file__).resolve().parents[2] / "examples" / "descriptors"

# Los ficheros numerados son descriptores enviables; los REFERENCIA_* son
# catalogos de consulta y se comprueban en su propia prueba.
DESCRIPTORS = sorted(p for p in DESCRIPTORS_DIR.glob("*.yaml") if p.name[0].isdigit())

# El de ELCM no es un Dataset Descriptor: solo lleva `experiment` y `dataset`.
ELCM_REQUESTS = {"05_experimento_elcm.yaml"}

IDS = [p.name for p in DESCRIPTORS]


def _load(path: Path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=JsonLikeLoader)


def _parse(path: Path):
    model = ElcmExperimentRequest if path.name in ELCM_REQUESTS else DatasetDescriptor
    return model.model_validate(_load(path))


def test_the_library_of_examples_exists() -> None:
    """El anteproyecto compromete cinco ejemplos concretos del descriptor."""
    assert len(DESCRIPTORS) >= 5


@pytest.mark.parametrize("path", DESCRIPTORS, ids=IDS)
def test_example_validates_against_its_model(path: Path) -> None:
    _parse(path)


@pytest.mark.parametrize("path", DESCRIPTORS, ids=IDS)
def test_example_has_no_empty_fields(path: Path) -> None:
    """Un `campo: ""` en un ejemplo se copia y produce un 400 al enviarlo."""
    reject_empty_strings_or_raise(_parse(path))


@pytest.mark.parametrize("path", DESCRIPTORS, ids=IDS)
def test_example_components_exist_and_are_editable(path: Path) -> None:
    """Los componentes referenciados existen y sus campos estan en el overlay.

    Es lo que detecta un nombre mal escrito: `ueransim` no es un componente, son
    `ueransim_both` y `ueransim_split`.
    """
    parsed = _parse(path)
    if isinstance(parsed, DatasetDescriptor):
        validate_components_or_raise(parsed.infrastructure)


@pytest.mark.parametrize("path", DESCRIPTORS, ids=IDS)
def test_example_passes_the_elcm_preflight(path: Path) -> None:
    """Los TestCases y UEs existen, y los modos de dataset tienen quien los surta."""
    parsed = _parse(path)
    if parsed.experiment is not None:
        preflight.validate_elcm_request(parsed.experiment, parsed.dataset)


@pytest.mark.parametrize("path", DESCRIPTORS, ids=IDS)
def test_example_is_documented_with_comments(path: Path) -> None:
    """Los comentarios son la razon de que el descriptor sea YAML y no JSON."""
    assert path.read_text(encoding="utf-8").lstrip().startswith("#")


@pytest.mark.parametrize("path", DESCRIPTORS, ids=IDS)
def test_example_is_a_single_document(path: Path) -> None:
    """La API rechaza los multi-documento; los ejemplos no deben ensenar eso."""
    assert len(list(yaml.safe_load_all(path.read_text(encoding="utf-8")))) == 1


def test_the_full_schema_example_covers_every_top_level_key() -> None:
    """00_esquema_completo debe ensenar TODO lo que admite el descriptor.

    Si se anade un campo al modelo y no aparece ahi, el esquema deja de serlo.
    """
    schema = _load(DESCRIPTORS_DIR / "00_esquema_completo.yaml")

    assert set(schema) == set(DatasetDescriptor.model_fields)
    assert set(schema["infrastructure"]) == set(
        DatasetDescriptor.model_fields["infrastructure"].annotation.model_fields
    )
    assert set(schema["experiment"]) == set(
        DatasetDescriptor.model_fields["experiment"].annotation.__args__[0].model_fields
    )
    assert set(schema["dataset"]) == set(
        DatasetDescriptor.model_fields["dataset"].annotation.model_fields
    )


def test_the_full_schema_example_shows_a_component_without_values() -> None:
    """El caso `componente:` vacio es el que mas confunde al venir de JSON."""
    schema = _load(DESCRIPTORS_DIR / "00_esquema_completo.yaml")

    assert None in schema["infrastructure"]["component"].values()
