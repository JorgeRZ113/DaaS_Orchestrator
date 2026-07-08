from pydantic import ValidationError

from app.models import DatasetDescriptor, ExecutionRecord, ExecutionState
from app.utils.ytt_renderer import build_tnlcm_values, resolve_template_path


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
                "base": {
                    "monitoring": {
                        "influxdb_user": "admin",
                        "influxdb_password": "adminadmin",
                        "grafana_password": "adminadmin",
                    }
                },
                "open5gs": {"open5gs": {"vm_size": "large"}},
                "network": {"network": {"n2_first_ip": "10.20.20.1"}},
            },
        },
        experiment={"name": "exp-demo", "testcase_paths": ["TestCase_ping.yml"]},
    )

    # Request values specific for the base template (only editable fields accepted)
    values = descriptor.tnlcm_data_values(template_ref="TNLCM/base_tnlcm_descriptor.yaml")

    assert values["monitoring"]["influxdb_user"] == "admin"
    assert values["monitoring"]["influxdb_password"] == "adminadmin"
    assert values["monitoring"]["grafana_password"] == "adminadmin"


def test_legacy_base_template_alias_still_resolves() -> None:
    resolved = resolve_template_path("TNLCM/tnlcm_descriptor_base.yaml", category="TNLCM")

    assert resolved is not None
    assert resolved.name == "base_tnlcm_descriptor.yaml"


def test_compound_ueransim_template_resolves_generically() -> None:
    resolved = resolve_template_path(
        "ueransim_both_sample_tnlcm_descriptor.yaml",
        category="TNLCM",
    )

    assert resolved is not None
    assert resolved.name == "ueransim_both_sample_tnlcm_descriptor.yaml"


def test_dataset_descriptor_auto_groups_flat_component_fields() -> None:
    """Test that fields sent directly in component.base are grouped under their overlay sections."""
    descriptor = DatasetDescriptor(
        infrastructure={
            "name": "tn-demo-flat",
            "descriptor_path": "TNLCM/base_tnlcm_descriptor.yaml",
            "component": {
                "base": {
                    "influxdb_user": "admin",
                    "influxdb_password": "adminadmin",
                    "grafana_password": "adminadmin",
                }
            },
        },
        experiment={"name": "exp-demo", "testcase_paths": ["TestCase_ping.yml"]},
    )

    # Request values for base template
    values = descriptor.tnlcm_data_values(template_ref="TNLCM/base_tnlcm_descriptor.yaml")

    # Fields should be auto-grouped under 'monitoring' section
    assert "monitoring" in values
    assert values["monitoring"]["influxdb_user"] == "admin"
    assert values["monitoring"]["influxdb_password"] == "adminadmin"
    assert values["monitoring"]["grafana_password"] == "adminadmin"


def test_dataset_descriptor_extracts_flat_mongodb_fields_only_if_editable() -> None:
    descriptor = DatasetDescriptor(
        infrastructure={
            "name": "tn-demo-mongo",
            "component": {
                "mongodb": {
                    "user": "mongo-user",
                    "password": "mongo-pass",
                    "version": "9.0",
                }
            },
        },
        experiment={"name": "exp-demo", "testcase_paths": ["TestCase_ping.yml"]},
    )

    values = descriptor.tnlcm_data_values(template_ref="TNLCM/mongodb_sample_tnlcm_descriptor.yaml")

    assert values["mongodb"]["user"] == "mongo-user"
    assert values["mongodb"]["password"] == "mongo-pass"
    assert "version" not in values["mongodb"]


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
