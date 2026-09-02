"""El YAML que genera la UI tiene que seguir validando contra el modelo del servidor.

La UI es un proceso aparte que habla HTTP, asi que nada la ata al esquema salvo
esta prueba: si alguien anade un campo obligatorio a `DatasetDescriptor` o
renombra un valor de `dataset.output`, el formulario seguiria produciendo un
fichero que la API rechaza y no habria forma de enterarse hasta usarlo.

Se prueba `ui/descriptor.py` y no `ui/streamlit_app.py` porque el primero no
importa Streamlit: son funciones puras y no hace falta un ScriptRunContext.
"""

import itertools
import sys
from pathlib import Path
from typing import get_args

import pytest

from fastapi import HTTPException
from pydantic import ValidationError

from app.api.body_formats import parse_yaml_descriptor
from app.api.schemas.requests import ElcmExperimentRequest
from app.api.validation import validate_components_or_raise
from app.domain.component_contract import extract_component_template_values
from app.domain.descriptor import (
    DATASET_MODE_VARIABLES,
    DatasetDescriptor,
    DatasetOutput,
    DatasetRequest,
    InfrastructureConfig,
)
from app.rendering.overlays import overlay_editable_fields_for_template
from app.rendering.paths import resolve_template_path
from app.rendering.tnlcm.overlay import COMPONENT_PARAMETER_MAPPING

# `ui/` es un directorio de scripts, no un paquete instalado: `streamlit run` lo
# pone en sys.path por si mismo y por eso sus modulos se importan planos.
UI_DIR = Path(__file__).resolve().parents[2] / "ui"
sys.path.insert(0, str(UI_DIR))

descriptor = pytest.importorskip("descriptor", reason="la UI no esta en el arbol")

# El cliente HTTP ya no vive bajo `ui/`: lo comparten la UI y el CLI, asi que
# se importa como cualquier modulo de `app/` y no necesita el sys.path de arriba.
from app import client as api_client  # noqa: E402

COMPONENTS = descriptor.list_components()

# Las cinco variables globales, sin importar a que modo pertenezcan.
DATASET_MODE_VARIABLES_BY_NAME = sorted(
    {name for names in DATASET_MODE_VARIABLES.values() for name in names}
)


def _fields_by_label(component: str) -> dict[str, object]:
    """Los campos de un componente indexados por su etiqueta en la interfaz."""
    return {field.label: field for field in descriptor.component_fields(component)}


def _editable_by_section(component: str) -> dict[str, set[str]]:
    """Lo que el SERVIDOR considera editable, leido de los overlays."""
    candidate = (
        "base_tnlcm_descriptor.yaml"
        if component == "base"
        else f"{component}_sample_tnlcm_descriptor.yaml"
    )
    path = resolve_template_path(candidate, category="TNLCM")
    assert path is not None, f"sin plantilla para {component}"
    return overlay_editable_fields_for_template(str(path), category="TNLCM")


def _base_component() -> dict[str, object]:
    """El bloque `component` con los tres obligatorios de `base` rellenos."""
    fields = _fields_by_label("base")
    return descriptor.build_component(
        ["base"],
        {
            fields["influxdb_user"]: "admin",
            fields["influxdb_password"]: "adminadmin",
            fields["grafana_password"]: "adminadmin",
        },
    )


def _descriptor_yaml(**overrides):
    """El descriptor que sale del formulario con sus valores por defecto."""
    defaults = dict(
        name="tn-demo",
        component=_base_component(),
        parameters={"library_reference_type": "branch", "library_reference_value": "develop"},
        experiment=descriptor.build_experiment("exp-demo", "TC_1_Preflight.yml", ""),
        dataset=descriptor.build_dataset(["logs"], {}),
        auto_start_elcm=True,
        ephemeral_tn=False,
    )
    defaults.update(overrides)
    return descriptor.to_yaml(descriptor.build_descriptor(**defaults))


def test_generated_descriptor_validates_against_the_server_model() -> None:
    model = DatasetDescriptor.model_validate(parse_yaml_descriptor(_descriptor_yaml()))

    assert model.infrastructure.name == "tn-demo"
    assert model.experiment.testcase_paths == ["TC_1_Preflight.yml"]
    assert model.dataset.output == ["logs"]


def test_component_without_values_is_read_as_use_the_defaults() -> None:
    """Es como la UI emite los componentes que solo se nombran.

    Ojo: esto solo cubre la coercion del modelo. Para los componentes CON campos
    obligatorios no basta con nombrarlos, ver
    `test_naming_a_component_with_required_fields_is_not_enough`.
    """
    yaml_text = _descriptor_yaml(
        component=descriptor.build_component(["vnet", "ueransim_both"], {})
    )

    model = DatasetDescriptor.model_validate(parse_yaml_descriptor(yaml_text))

    assert model.infrastructure.component == {"vnet": {}, "ueransim_both": {}}


@pytest.mark.parametrize("component", COMPONENTS)
def test_every_field_the_ui_offers_is_accepted_by_the_server(component: str) -> None:
    """Lo que el formulario deja tocar tiene que pasar la validacion real.

    La UI saca los campos del catalogo publicado y el servidor de los overlays;
    nada ata las dos listas salvo esta prueba. Si alguien anade un campo a un
    overlay sin reflejarlo en el catalogo, o al reves, aqui se ve.
    """
    fields = descriptor.component_fields(component)
    payload = descriptor.build_component(
        [component], {field: f"v-{field.name}" for field in fields}
    )

    validate_components_or_raise(
        InfrastructureConfig.model_validate({"name": "tn", "component": payload})
    )


def test_ambiguous_fields_land_in_the_section_the_user_picked() -> None:
    """El motivo real de emitirlos anidados, y que el test anterior no puede ver.

    En plano el backend NO los rechaza: los enruta en silencio a una sola de las
    secciones, asi que `int_p4_sw.name` acabaria siempre en `vm` y `network.name`
    seria inalcanzable. Solo mirando el `extracted` se distingue.
    """
    fields = _fields_by_label("int_p4_sw")
    payload = descriptor.build_component(
        ["int_p4_sw"], {fields["network.name"]: "vnet-a", fields["vm.name"]: "sw-1"}
    )

    extracted, invalids = extract_component_template_values(
        "int_p4_sw", payload["int_p4_sw"], _editable_by_section("int_p4_sw")
    )

    assert invalids == []
    assert extracted["network"]["name"] == "vnet-a"
    assert extracted["vm"]["name"] == "sw-1"


def test_unambiguous_fields_travel_flat() -> None:
    """El formato plano es el de los ejemplos y el README; no anidar de mas."""
    fields = _fields_by_label("base")

    assert descriptor.build_component(["base"], {fields["influxdb_user"]: "admin"}) == {
        "base": {"influxdb_user": "admin"}
    }


@pytest.mark.parametrize("component", COMPONENTS)
def test_required_markers_match_the_servers_required_list(component: str) -> None:
    """El asterisco de la interfaz sale del catalogo; tiene que decir la verdad."""
    offered = {field.name for field in descriptor.component_fields(component) if field.required}
    declared = set(COMPONENT_PARAMETER_MAPPING.get(component, {}).get("required", []))

    assert offered == declared


def test_naming_a_component_with_required_fields_is_not_enough() -> None:
    """`mongodb:` a secas es un 400, no «usa los defaults»: la UI debe pedirlos."""
    assert descriptor.missing_required(["mongodb"], {}) == {
        "mongodb": ["database", "express_password", "express_user", "password", "user"]
    }

    with pytest.raises(HTTPException):
        validate_components_or_raise(
            InfrastructureConfig.model_validate({"name": "tn", "component": {"mongodb": None}})
        )


def test_component_without_required_fields_needs_nothing() -> None:
    """El caso contrario: elegir `vnet` y no tocar nada es valido y se despliega."""
    assert descriptor.missing_required(["vnet"], {}) == {}

    validate_components_or_raise(
        InfrastructureConfig.model_validate(
            {"name": "tn", "component": descriptor.build_component(["vnet"], {})}
        )
    )


def test_blank_values_do_not_count_as_provided() -> None:
    """Un obligatorio con espacios sigue faltando, y no se cuela en el YAML."""
    fields = _fields_by_label("mongodb")
    values = {fields["user"]: "   ", fields["database"]: "testing"}

    assert "user" in descriptor.missing_required(["mongodb"], values)["mongodb"]
    assert descriptor.build_component(["mongodb"], values) == {"mongodb": {"database": "testing"}}


def test_empty_dataset_variables_are_omitted_not_emitted_as_null() -> None:
    """Un `null` explicito no dice nada que no diga la ausencia de la clave."""
    dataset = descriptor.build_dataset(
        ["csv"],
        {"measurement": "OPEN5GS_KPIS", "influx_host": "   ", "influx_port": None},
    )

    assert dataset == {"output": ["csv"], "measurement": "OPEN5GS_KPIS"}


def test_generated_elcm_request_validates_against_the_server_model() -> None:
    yaml_text = descriptor.to_yaml(
        descriptor.build_elcm_request(
            experiment=descriptor.build_experiment(
                "exp-2",
                "TC_5_Flujo_Variables.yml\n TC_1_Preflight.yml ",
                "UE_5_Flujo_Variables.yml",
            ),
            dataset=descriptor.build_dataset(["logs"], {}),
        )
    )

    model = ElcmExperimentRequest.model_validate(parse_yaml_descriptor(yaml_text))

    # Los espacios sobrantes del textarea se recortan y las lineas vacias caen.
    assert model.experiment.testcase_paths == ["TC_5_Flujo_Variables.yml", "TC_1_Preflight.yml"]
    assert model.experiment.ues_paths == ["UE_5_Flujo_Variables.yml"]


def test_ui_offers_every_output_format_the_model_accepts() -> None:
    """El multiselect de `dataset.output` no puede quedarse corto en silencio."""
    assert set(api_client.DATASET_OUTPUTS) == set(get_args(DatasetOutput))


@pytest.mark.parametrize(
    "outputs",
    [
        subset
        for size in range(1, len(api_client.DATASET_OUTPUTS) + 1)
        for subset in itertools.combinations(api_client.DATASET_OUTPUTS, size)
    ],
)
def test_ui_offers_exactly_the_variables_the_model_accepts(outputs: tuple[str, ...]) -> None:
    """Sobre los 31 subconjuntos de `output`, campo a campo.

    Ofrecer de menos esconde una funcion que el servidor si admite; ofrecer de
    mas lleva al usuario a un 422. La unica forma de no equivocarse es
    preguntarle al modelo por cada combinacion.
    """
    offered = set(api_client.variables_for_outputs(outputs))

    for variable in DATASET_MODE_VARIABLES_BY_NAME:
        value = 8086 if variable == "influx_port" else "x"
        try:
            DatasetRequest(output=list(outputs), **{variable: value})
        except ValidationError:
            assert variable not in offered, f"{variable} no vale con {outputs}"
        else:
            assert variable in offered, f"{variable} si vale con {outputs} y no se ofrece"


def test_ui_mirrors_the_variable_to_mode_table_of_the_model() -> None:
    """El aviso previo de la UI se apoya en esta tabla; si se desvia, miente."""
    mirrored = {
        variable: set(owners) for variable, owners in api_client.DATASET_MODE_VARIABLES.items()
    }
    expected: dict[str, set[str]] = {}
    for mode, names in DATASET_MODE_VARIABLES.items():
        for name in names:
            expected.setdefault(name, set()).add(mode)

    assert mirrored == expected


def test_bundled_examples_are_loadable_and_exclude_the_component_catalog() -> None:
    names = descriptor.list_examples()

    assert "REFERENCIA_componentes.yaml" not in names
    assert descriptor.ELCM_EXAMPLE in names
    for name in names:
        assert descriptor.read_example(name).strip(), f"{name} esta vacio"


def test_component_picker_matches_the_published_catalog() -> None:
    components = descriptor.list_components()

    assert "base" in components
    assert "ueransim_both" in components
