"""Validacion de los modelos del DatasetDescriptor. Sin I/O: solo Pydantic."""

from pydantic import ValidationError

from app.domain.descriptor import DatasetDescriptor, DatasetRequest
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord, ExperimentRun


def test_dataset_descriptor_uses_logs_as_default_output() -> None:
    descriptor = DatasetDescriptor(
        infrastructure={"name": "tn-demo", "descriptor_path": "tn_descriptor_elcm.yaml"},
        experiment={"name": "exp-demo", "testcase_paths": ["TC_ping.yml"]},
    )

    assert descriptor.dataset.output == ["logs"]
    assert descriptor.dataset.wants("logs")
    assert descriptor.experiment.testcase_paths == ["TC_ping.yml"]


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


# --- componente nombrado sin valores: «despliegalo con sus defaults» ---


def test_component_without_values_is_accepted_as_empty_mapping() -> None:
    """En JSON se escribe `{}`; el modelo debe tratarlo como «usa los defaults»."""
    descriptor = DatasetDescriptor(
        infrastructure={"name": "tn-demo", "component": {"base": {}, "ueransim_both": {}}},
        experiment={"name": "exp-demo", "testcase_paths": ["TC_ping.yml"]},
    )

    assert descriptor.infrastructure.component["ueransim_both"] == {}


def test_component_left_empty_in_yaml_means_the_same_as_an_empty_mapping() -> None:
    """En YAML lo natural es `ueransim_both:` a secas, que llega como None.

    Antes se rechazaba con un 422, de modo que un descriptor valido en JSON
    dejaba de serlo al escribirlo en YAML pese a decir exactamente lo mismo.
    """
    from_yaml = DatasetDescriptor(
        infrastructure={"name": "tn-demo", "component": {"base": {}, "ueransim_both": None}},
        experiment={"name": "exp-demo", "testcase_paths": ["TC_ping.yml"]},
    )
    from_json = DatasetDescriptor(
        infrastructure={"name": "tn-demo", "component": {"base": {}, "ueransim_both": {}}},
        experiment={"name": "exp-demo", "testcase_paths": ["TC_ping.yml"]},
    )

    assert from_yaml.model_dump() == from_json.model_dump()


def test_component_with_values_is_left_untouched() -> None:
    """La coercion solo debe afectar a los componentes sin valores."""
    descriptor = DatasetDescriptor(
        infrastructure={
            "name": "tn-demo",
            "component": {"base": {"influxdb_user": "admin"}, "vnet": None},
        },
        experiment={"name": "exp-demo", "testcase_paths": ["TC_ping.yml"]},
    )

    assert descriptor.infrastructure.component == {
        "base": {"influxdb_user": "admin"},
        "vnet": {},
    }


def test_component_is_still_optional() -> None:
    descriptor = DatasetDescriptor(
        infrastructure={"name": "tn-demo"},
        experiment={"name": "exp-demo", "testcase_paths": ["TC_ping.yml"]},
    )

    assert descriptor.infrastructure.component is None
