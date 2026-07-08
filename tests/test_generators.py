import json
from pathlib import Path

import pytest
import yaml

from app import generators
from app.config import settings
from app.models import ExperimentConfig, InfrastructureConfig
from app.utils.telemetry import telemetry


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
    output_path = await generators.generate_tnlcm_descriptor(
        InfrastructureConfig(name="tn-demo", descriptor_path="TNLCM/base_tnlcm_descriptor.yaml"),
        execution_id="exec-tn",
    )

    output_file = Path(output_path)
    assert output_file.exists()
    assert output_file.parent.name == "archivos_generados"

    payload = yaml.safe_load(output_file.read_text(encoding="utf-8"))
    assert "trial_network" in payload


@pytest.mark.asyncio
async def test_generate_tnlcm_descriptor_extracts_flat_mongodb_fields_with_defaults():
    output_path = await generators.generate_tnlcm_descriptor(
        InfrastructureConfig(
            name="tn-demo",
            descriptor_path="TNLCM/base_tnlcm_descriptor.yaml",
            component={
                "mongodb": {
                    "user": "mongo-user",
                    "password": "mongo-pass",
                    "database": "mongo-db",
                }
            },
        ),
        execution_id="exec-tn-mongo",
    )

    payload = yaml.safe_load(Path(output_path).read_text(encoding="utf-8"))
    trial_network = payload["trial_network"]
    mongodb_component = trial_network["mongodb-v8"]
    mongodb_input = mongodb_component["input"]

    assert mongodb_input["one_mongodb_user"] == "mongo-user"
    assert mongodb_input["one_mongodb_password"] == "mongo-pass"
    assert mongodb_input["one_mongodb_database"] == "mongo-db"
    # version is not editable in overlay and should remain default from overlay/template
    assert mongodb_input["one_mongodb_version"] == "8.0"


@pytest.mark.asyncio
async def test_generate_tnlcm_descriptor_resolves_compound_ueransim_template(monkeypatch, tmp_path):
    original_resolve_template_path = generators.resolve_template_path

    def _resolve_template_path(template_ref: str, category: str | None = None):
        if template_ref == "ueransim_both_sample_tnlcm_descriptor.yaml":
            return original_resolve_template_path(
                "TNLCM/ueransim_both_sample_tnlcm_descriptor.yaml",
                category="TNLCM",
            )
        return original_resolve_template_path(template_ref, category=category)

    monkeypatch.setattr("app.generators.resolve_template_path", _resolve_template_path)

    output_path = await generators.generate_tnlcm_descriptor(
        InfrastructureConfig(
            name="tn-demo-ueransim",
            component={"ueransim_both": {}},
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
async def test_generate_testcase_and_experiment_descriptor(monkeypatch, tmp_path):
    testcase_template = tmp_path / "TPL_Run_Message.yml"
    testcase_template.write_text("testcase: demo\n", encoding="utf-8")

    experiment_template = tmp_path / "template_experiment_descriptor.json"
    experiment_template.write_text("{}\n", encoding="utf-8")

    def _resolve_template_path(template_ref: str, category: str | None = None):
        if template_ref.endswith("template_experiment_descriptor.json"):
            return experiment_template
        return testcase_template

    def _render_with_ytt(values, template_ref: str, category: str | None = None):
        template_path = Path(template_ref)
        if template_path.suffix == ".json":
            return json.dumps(
                {
                    "Application": values["Application"],
                    "TestCases": values["TestCases"],
                    "UEs": values["UEs"],
                },
                indent=4,
                ensure_ascii=False,
            )

        return yaml.safe_dump(
            {
                "execution_id": values["execution_id"],
                "testcase_name": values["testcase_name"],
                "testcase_ref": values["testcase_ref"],
            },
            sort_keys=False,
            allow_unicode=True,
        )

    monkeypatch.setattr("app.generators.resolve_template_path", _resolve_template_path)
    monkeypatch.setattr("app.generators.render_with_ytt", _render_with_ytt)

    testcase_one = await generators.generate_testcase(
        "TPL_Run_Message.yml",
        execution_id="exec-elcm",
        output_index=0,
    )
    testcase_two = await generators.generate_testcase(
        "TPL_Run_Dummy.yml",
        execution_id="exec-elcm",
        output_index=1,
    )

    experiment_path = await generators.generate_experiment_descriptor(
        ExperimentConfig(
            name="exp-demo", testcase_paths=["TPL_Run_Message.yml", "TPL_Run_Dummy.yml"]
        ),
        [testcase_one, testcase_two],
        execution_id="exec-elcm",
    )

    testcase_one_path = Path(testcase_one)
    testcase_two_path = Path(testcase_two)
    assert testcase_one_path.exists()
    assert testcase_two_path.exists()
    assert testcase_one_path.name == "testcase_001.yml"
    assert testcase_two_path.name == "testcase_002.yml"

    experiment_file = Path(experiment_path)
    assert experiment_file.exists()
    payload = json.loads(experiment_file.read_text(encoding="utf-8"))
    assert payload["Application"] == "exp-demo"
    assert payload["TestCases"] == ["testcase_001", "testcase_002"]
