from pydantic import ValidationError

from app.models import (
    DatasetDescriptor,
    DatasetRequest,
    ExecutionRecord,
    ExecutionState,
    ExperimentRun,
)
from app.utils.ytt_renderer import build_tnlcm_values, resolve_template_path


def test_dataset_descriptor_uses_logs_as_default_output() -> None:
    descriptor = DatasetDescriptor(
        infrastructure={"name": "tn-demo", "descriptor_path": "tn_descriptor_elcm.yaml"},
        experiment={"name": "exp-demo", "testcase_paths": ["TC_ping.yml"]},
    )

    assert descriptor.dataset.output == ["logs"]
    assert descriptor.dataset.wants("logs")
    assert descriptor.experiment.testcase_paths == ["TC_ping.yml"]


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
        experiment={"name": "exp-demo", "testcase_paths": ["TC_ping.yml"]},
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
        experiment={"name": "exp-demo", "testcase_paths": ["TC_ping.yml"]},
    )

    # Request values for base template
    values = descriptor.tnlcm_data_values(template_ref="TNLCM/base_tnlcm_descriptor.yaml")

    # Fields should be auto-grouped under 'monitoring' section
    assert "monitoring" in values
    assert values["monitoring"]["influxdb_user"] == "admin"
    assert values["monitoring"]["influxdb_password"] == "adminadmin"
    assert values["monitoring"]["grafana_password"] == "adminadmin"


def test_dataset_descriptor_extracts_flat_mongodb_fields_only_if_editable() -> None:
    # Fields declared in the overlay (regardless of their default value) are
    # accepted here; whether they are actually mandatory/optional is decided
    # later by COMPONENT_PARAMETER_MAPPING at generation time, not here (see
    # overlay_editable_fields_for_template docstring). Only fields that are
    # not declared in the overlay at all get dropped.
    descriptor = DatasetDescriptor(
        infrastructure={
            "name": "tn-demo-mongo",
            "component": {
                "mongodb": {
                    "user": "mongo-user",
                    "password": "mongo-pass",
                    "not_an_overlay_field": "should-be-dropped",
                }
            },
        },
        experiment={"name": "exp-demo", "testcase_paths": ["TC_ping.yml"]},
    )

    values = descriptor.tnlcm_data_values(template_ref="TNLCM/mongodb_sample_tnlcm_descriptor.yaml")

    assert values["mongodb"]["user"] == "mongo-user"
    assert values["mongodb"]["password"] == "mongo-pass"
    assert "not_an_overlay_field" not in values["mongodb"]


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
            experiment={"name": "exp-demo", "testcase_paths": ["TC_ping.yml"]},
            dataset={"output": "zip"},
        )
        raise AssertionError("DatasetDescriptor should reject unsupported dataset.output")
    except ValidationError:
        pass


def test_dataset_output_accepts_single_string() -> None:
    dataset = DatasetRequest(output="csv")
    assert dataset.output == ["csv"]
    assert dataset.wants("csv")
    assert not dataset.wants("logs")


def test_dataset_output_accepts_list_and_dedups_preserving_order() -> None:
    dataset = DatasetRequest(output=["csv", "logs", "csv", "raw"])
    assert dataset.output == ["csv", "logs", "raw"]


def test_dataset_output_is_case_insensitive_and_trimmed() -> None:
    dataset = DatasetRequest(output=[" LOGS ", "Dashboard"])
    assert dataset.output == ["logs", "dashboard"]


def test_dataset_output_accepts_all_four_formats() -> None:
    dataset = DatasetRequest(output=["logs", "csv", "dashboard", "raw"])
    assert dataset.output == ["logs", "csv", "dashboard", "raw"]


def test_dataset_output_rejects_unknown_value_in_list() -> None:
    try:
        DatasetRequest(output=["logs", "zip"])
        raise AssertionError("DatasetRequest should reject unknown output values")
    except ValidationError:
        pass


def test_dataset_output_rejects_empty_list() -> None:
    try:
        DatasetRequest(output=[])
        raise AssertionError("DatasetRequest should reject an empty output list")
    except ValidationError:
        pass


def test_execution_record_default_lists_are_initialized() -> None:
    record = ExecutionRecord(execution_id="exec-1", status=ExecutionState.pending)

    assert record.experiment_ids == []
    assert record.artifacts == []
    assert record.dataset_output == ["logs"]


# --- Variables globales del bloque dataset (por modo de salida) ---


def test_dataset_variables_are_collected_without_the_unset_ones() -> None:
    dataset = DatasetRequest(output=["csv"], measurement="OPEN5GS_KPIS", influx_port=8087)

    assert dataset.variables() == {"influx_port": 8087, "measurement": "OPEN5GS_KPIS"}


def test_dataset_variables_default_to_empty() -> None:
    assert DatasetRequest(output=["logs"]).variables() == {}


def test_dataset_accepts_variables_of_every_requested_mode() -> None:
    dataset = DatasetRequest(
        output=["csv", "dashboard", "raw"],
        measurement="OPEN5GS_KPIS",
        influx_host="10.11.27.5",
        influx_port=8086,
        influx_bucket="testing",
        panel_interval="10s",
    )

    assert dataset.influx_host == "10.11.27.5"
    assert dataset.panel_interval == "10s"


def test_dataset_rejects_variable_whose_mode_is_not_requested() -> None:
    # influx_host solo aplica a 'csv': pedirlo con output=['logs'] casi siempre
    # significa que se olvido el modo, asi que se rechaza en vez de ignorarlo.
    try:
        DatasetRequest(output=["logs"], influx_host="10.11.27.5")
        raise AssertionError("DatasetRequest should reject variables of inactive modes")
    except ValidationError as exc:
        assert "influx_host" in str(exc)


def test_dataset_accepts_variable_shared_by_another_active_mode() -> None:
    # measurement pertenece a csv, dashboard y raw: basta con que uno este activo.
    dataset = DatasetRequest(output=["raw"], measurement="OPEN5GS_KPIS")

    assert dataset.measurement == "OPEN5GS_KPIS"


def test_dataset_rejects_dashboard_variable_without_dashboard_output() -> None:
    try:
        DatasetRequest(output=["csv"], panel_interval="10s")
        raise AssertionError("panel_interval requires dataset.output to include 'dashboard'")
    except ValidationError as exc:
        assert "panel_interval" in str(exc)


def test_experiment_run_and_record_carry_dataset_variables() -> None:
    record = ExecutionRecord(
        execution_id="exec-1",
        status=ExecutionState.pending,
        dataset_variables={"measurement": "OPEN5GS_KPIS"},
    )

    assert record.dataset_variables == {"measurement": "OPEN5GS_KPIS"}
    assert ExperimentRun(name="exp").dataset_variables == {}
