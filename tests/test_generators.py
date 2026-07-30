import json
from pathlib import Path

import pytest
import yaml

from app import elcm
from app.config import settings
from app.generators.tnlcm_renderer import generate_tnlcm_descriptor
from app.models import ExperimentConfig, InfrastructureConfig
from app.utils.telemetry import telemetry

BASE_COMPONENT_VALUES = {
    "influxdb_user": "influx-user",
    "influxdb_password": "influx-pass",
    "grafana_password": "grafana-pass",
}


@pytest.fixture(autouse=True)
def _isolate_generator_state(tmp_path):
    previous_artifacts_dir = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)
    telemetry.reset()

    yield

    telemetry.reset()
    settings.artifacts_dir = previous_artifacts_dir


@pytest.mark.asyncio
async def test_generate_tnlcm_descriptor_creates_yaml_in_generated_dir():
    output_path = await generate_tnlcm_descriptor(
        InfrastructureConfig(name="tn-demo", component={"base": BASE_COMPONENT_VALUES}),
        execution_id="exec-tn",
    )

    output_file = Path(output_path)
    assert output_file.exists()
    assert output_file.parent.name == "archivos_generados"

    payload = yaml.safe_load(output_file.read_text(encoding="utf-8"))
    assert "trial_network" in payload


@pytest.mark.asyncio
async def test_generate_tnlcm_descriptor_extracts_flat_mongodb_fields_with_defaults():
    # NOTE: COMPONENT_PARAMETER_MAPPING["mongodb"] currently only allows
    # "database"/"replica_set" (pendiente completar el mapeo para
    # mongodb/redis). "user"/"password" are not yet wired up.
    output_path = await generate_tnlcm_descriptor(
        InfrastructureConfig(
            name="tn-demo",
            component={
                "base": BASE_COMPONENT_VALUES,
                "mongodb": {"database": "mongo-db"},
            },
        ),
        execution_id="exec-tn-mongo",
    )

    payload = yaml.safe_load(Path(output_path).read_text(encoding="utf-8"))
    trial_network = payload["trial_network"]
    mongodb_component = trial_network["mongodb-v8"]
    mongodb_input = mongodb_component["input"]

    assert mongodb_input["one_mongodb_database"] == "mongo-db"
    # user/password are not yet mapped, so they keep the overlay defaults
    assert mongodb_input["one_mongodb_user"] == ""
    assert mongodb_input["one_mongodb_password"] == ""
    # version is not editable in overlay and should remain default from overlay/template
    assert mongodb_input["one_mongodb_version"] == "8.0"


@pytest.mark.asyncio
async def test_generate_tnlcm_descriptor_resolves_compound_ueransim_template():
    output_path = await generate_tnlcm_descriptor(
        InfrastructureConfig(
            name="tn-demo-ueransim",
            component={"base": BASE_COMPONENT_VALUES, "ueransim_both": {}},
        ),
        execution_id="exec-tn-ueransim",
    )

    payload = yaml.safe_load(Path(output_path).read_text(encoding="utf-8"))
    trial_network = payload["trial_network"]

    assert "ueransim-both" in trial_network
    assert trial_network["ueransim-both"]["type"] == "ueransim"
    assert trial_network["ueransim-both"]["name"] == "both"
    assert trial_network["ueransim-both"]["dependencies"] == ["open5gs_vm-core"]


@pytest.mark.asyncio
async def test_experiment_descriptor_references_testcases_by_internal_name(tmp_path):
    # Los TestCases se toman verbatim (no se re-renderizan) y el descriptor los
    # referencia por su Name interno, no por el nombre de fichero.
    tc_one = tmp_path / "TestCase_ping.yml"
    tc_one.write_text("Version: 2\nName: Test_ping\nStandard: True\n", encoding="utf-8")
    tc_two = tmp_path / "otro_fichero.yml"
    tc_two.write_text("Version: 2\nName: TC_Custom\nStandard: True\n", encoding="utf-8")

    assert elcm.resolve_testcase_file(str(tc_one)) == tc_one.resolve()
    assert elcm.extract_testcase_name(str(tc_one)) == "Test_ping"

    experiment_path = await elcm.generate_experiment_descriptor(
        ExperimentConfig(name="exp-demo", testcase_paths=["a", "b"], ues_paths=["ue-1"]),
        [str(tc_one), str(tc_two)],
        execution_id="exec-elcm",
    )

    experiment_file = Path(experiment_path)
    assert experiment_file.exists()
    assert experiment_file.parent.name == "archivos_generados"
    payload = json.loads(experiment_file.read_text(encoding="utf-8"))
    assert payload["Application"] == "exp-demo"
    # Referencia por Name interno (no por stem de fichero).
    assert payload["TestCases"] == ["Test_ping", "TC_Custom"]
    assert payload["UEs"] == ["ue-1"]


def test_resolve_testcase_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        elcm.resolve_testcase_file(str(tmp_path / "no_existe.yml"))


def test_extract_testcase_name_without_name_raises(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text("Version: 2\nSequence: []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        elcm.extract_testcase_name(str(bad))
