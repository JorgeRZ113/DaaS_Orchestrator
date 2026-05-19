from pydantic import ValidationError

from app.models import DatasetDescriptor, ExecutionRecord, ExecutionState
from app.utils.ytt_renderer import build_tnlcm_values


def test_dataset_descriptor_uses_logs_as_default_output() -> None:
    descriptor = DatasetDescriptor(
        infrastructure={"name": "tn-demo", "descriptor_path": "tn_descriptor_elcm.yaml"},
        experiment={"name": "exp-demo", "testcase_paths": ["TestCase_ping.yml"]},
    )

    assert descriptor.dataset.output == "logs"
    assert descriptor.experiment.testcase_paths == ["TestCase_ping.yml"]


def test_dataset_descriptor_merges_all_component_sections() -> None:
    descriptor = DatasetDescriptor(
        infrastructure={
            "name": "tn-demo",
            "descriptor_path": "TNLCM/base_tnlcm_descriptor.yaml",
            "component": {
                "base": {"monitoring": {"influxdb_version": "2.7.11"}},
                "open5gs": {"open5gs": {"vm_size": "large"}},
                "network": {"network": {"n2_first_ip": "10.20.20.1"}},
            },
        },
        experiment={"name": "exp-demo", "testcase_paths": ["TestCase_ping.yml"]},
    )

    values = descriptor.tnlcm_data_values()

    assert values["monitoring"]["influxdb_version"] == "2.7.11"
    assert values["open5gs"]["vm_size"] == "large"
    assert values["network"]["n2_first_ip"] == "10.20.20.1"


def test_build_tnlcm_values_normalizes_overlay_aliases() -> None:
    values = build_tnlcm_values(
        "TNLCM/vnet_sample_tnlcm_descriptor.yaml",
        {"network": {"first_ip": "10.20.20.1"}},
        category="TNLCM",
    )

    assert values["network"]["first_ip"] == "10.20.20.1"
    assert values["network"]["netmask"] == 24
    assert values["network"]["address_size"] == 100


def test_dataset_descriptor_rejects_unsupported_output() -> None:
    try:
        DatasetDescriptor(
            infrastructure={"name": "tn-demo", "descriptor_path": "tn_descriptor_elcm.yaml"},
            experiment={"name": "exp-demo", "testcase_paths": ["TestCase_ping.yml"]},
            dataset={"output": "zip"},
        )
        raise AssertionError("DatasetDescriptor should reject unsupported dataset.output")
    except ValidationError:
        pass


def test_execution_record_default_lists_are_initialized() -> None:
    record = ExecutionRecord(execution_id="exec-1", status=ExecutionState.pending)

    assert record.experiment_ids == []
    assert record.artifacts == []
