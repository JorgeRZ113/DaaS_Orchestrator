"""Contrato del Experiment Descriptor que el orquestador envia a ELCM.

`ExperimentDescriptor.validate()` (`Data/experiment_descriptor.py`) comprueba que
esten TODAS las claves de la lista: si falta una sola, el descriptor se marca
como invalido y el experimento no llega a arrancar. La plantilla de este
proyecto es el unico sitio donde se declaran, asi que se ata aqui.
"""

import json

import pytest

from app.rendering.paths import resolve_template_path

# Lista literal de `ExperimentDescriptor.validate()`. Son obligatorias TODAS,
# incluidas las que este proyecto no usa.
REQUIRED_KEYS: tuple[str, ...] = (
    "Version",
    "ExperimentType",
    "TestCases",
    "UEs",
    "Slice",
    "NSs",
    "ExclusiveExecution",
    "Scenario",
    "Automated",
    "ReservationTime",
    "Application",
    "Parameters",
    "Remote",
    "Extra",
)


@pytest.fixture(scope="module")
def descriptor() -> dict:
    path = resolve_template_path("ELCM/template_experiment_descriptor.json", category="ELCM")
    assert path is not None, "No se encuentra ELCM/template_experiment_descriptor.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_descriptor_declares_every_key_elcm_requires(descriptor: dict):
    missing = [key for key in REQUIRED_KEYS if key not in descriptor]
    assert not missing, (
        f"La plantilla no declara {missing}. ELCM valida la presencia de las 14 claves "
        f"y sin ellas el experimento no arranca."
    )


def test_parameters_exists_but_stays_empty(descriptor: dict):
    # 'Parameters' es el mecanismo de variables por experimento de ELCM: se lee
    # desde un TestCase con @[Params.X]. Este proyecto NO lo usa -todas las
    # variables entran por los ficheros UE- pero la clave tiene que existir
    # igualmente porque ELCM la exige. Debe quedarse vacia: rellenarla abriria un
    # segundo canal de variables en paralelo al de los UE.
    assert descriptor["Parameters"] == {}, (
        "El bloque 'Parameters' del Experiment Descriptor tiene que existir pero quedarse "
        "vacio: las variables del experimento van en los ficheros UE."
    )
