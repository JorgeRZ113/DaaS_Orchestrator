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
async def test_generate_testcase_and_experiment_descriptor():
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
        ExperimentConfig(name="exp-demo", testcase_paths=["TPL_Run_Message.yml", "TPL_Run_Dummy.yml"]),
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

