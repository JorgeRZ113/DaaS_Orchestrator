"""Extraccion de data values contra los overlays REALES de `templates/`.

No son unitarias: dependen de los ficheros `.overlay.yaml` del repositorio,
que es precisamente lo que se quiere verificar.
"""

from app.domain.descriptor import DatasetDescriptor
from app.rendering.overlays import build_tnlcm_values
from app.rendering.paths import resolve_template_path


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
