"""Que queda en artifacts/ segun el formato en que llego el descriptor.

La regla: el YAML se escribe siempre, porque es el formato que define el
descriptor; el JSON solo se anade cuando el JSON fue lo que se envio. Y si el
origen era YAML, se guarda el texto tal cual para no perder los comentarios.
"""

import json
import os

import pytest
import yaml

from app.domain.descriptor import DatasetDescriptor, DescriptorSource
from app.storage import artifacts

pytestmark = pytest.mark.usefixtures("isolate_artifacts_dir")


DESCRIPTOR_YAML = """\
# Despliegue minimo para la demo
infrastructure:
  name: tn_demo
  component:
    base:
      influxdb_user: admin   # usuario por defecto
experiment:
  name: demo
  testcase_paths:
    - TC_ping.yml
dataset:
  output: [logs]
"""


def _descriptor() -> DatasetDescriptor:
    return DatasetDescriptor.model_validate(yaml.safe_load(DESCRIPTOR_YAML))


def _files_in(execution_id: str) -> set[str]:
    return set(os.listdir(artifacts._artifact_base_dir(execution_id)))


def _read(execution_id: str, filename: str) -> str:
    path = os.path.join(artifacts._artifact_base_dir(execution_id), filename)
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def test_yaml_source_writes_only_yaml() -> None:
    source = DescriptorSource(format="yaml", raw=DESCRIPTOR_YAML)

    written = artifacts.persist_dataset_descriptor("tn_yaml", _descriptor(), source)

    assert _files_in("tn_yaml") == {"dataset_descriptor.yaml"}
    assert [os.path.basename(p) for p in written] == ["dataset_descriptor.yaml"]


def test_yaml_source_is_stored_verbatim_with_comments() -> None:
    """El valor de guardar YAML esta en los comentarios; reserializar los perderia."""
    source = DescriptorSource(format="yaml", raw=DESCRIPTOR_YAML)

    artifacts.persist_dataset_descriptor("tn_verbatim", _descriptor(), source)

    stored = _read("tn_verbatim", "dataset_descriptor.yaml")
    assert stored == DESCRIPTOR_YAML
    assert "# Despliegue minimo para la demo" in stored
    assert "# usuario por defecto" in stored


def test_json_source_writes_both_formats() -> None:
    written = artifacts.persist_dataset_descriptor(
        "tn_json", _descriptor(), DescriptorSource(format="json")
    )

    assert _files_in("tn_json") == {"dataset_descriptor.yaml", "dataset_descriptor.json"}
    assert [os.path.basename(p) for p in written] == [
        "dataset_descriptor.yaml",
        "dataset_descriptor.json",
    ]


def test_json_source_yaml_is_generated_and_equivalent() -> None:
    artifacts.persist_dataset_descriptor("tn_equiv", _descriptor(), DescriptorSource(format="json"))

    from_yaml = yaml.safe_load(_read("tn_equiv", "dataset_descriptor.yaml"))
    from_json = json.loads(_read("tn_equiv", "dataset_descriptor.json"))
    assert from_yaml == from_json


def test_missing_source_behaves_like_json() -> None:
    """Un llamante programatico que no informa el origen no debe perder el JSON."""
    written = artifacts.persist_dataset_descriptor("tn_default", _descriptor())

    assert _files_in("tn_default") == {"dataset_descriptor.yaml", "dataset_descriptor.json"}
    assert len(written) == 2


def test_generated_yaml_is_not_quoted_like_elcm() -> None:
    """El descriptor no pasa por `rendering.yaml_style`, que entrecomilla todo."""
    artifacts.persist_dataset_descriptor("tn_plain", _descriptor(), DescriptorSource(format="json"))

    assert 'name: "tn_demo"' not in _read("tn_plain", "dataset_descriptor.yaml")
    assert "name: tn_demo" in _read("tn_plain", "dataset_descriptor.yaml")


def test_descriptor_path_is_excluded_from_generated_forms() -> None:
    descriptor = DatasetDescriptor.model_validate(
        {
            "infrastructure": {"name": "tn_excl", "descriptor_path": "algo.yaml"},
            "experiment": {"name": "demo", "testcase_paths": ["TC_ping.yml"]},
        }
    )

    artifacts.persist_dataset_descriptor("tn_excl", descriptor, DescriptorSource(format="json"))

    stored = yaml.safe_load(_read("tn_excl", "dataset_descriptor.yaml"))
    assert "descriptor_path" not in stored["infrastructure"]


def test_roundtrip_through_load_dataset_descriptor() -> None:
    original = _descriptor()
    artifacts.persist_dataset_descriptor(
        "tn_round", original, DescriptorSource(format="yaml", raw=DESCRIPTOR_YAML)
    )

    assert artifacts.load_dataset_descriptor("tn_round").infrastructure.name == "tn_demo"


def test_generated_yaml_can_be_resent_as_is() -> None:
    """El fichero persistido tiene que servir para relanzar la misma ejecucion.

    Es lo que hara cualquiera que quiera repetir un experimento: coger el YAML de
    `artifacts/` y reenviarlo. Si el volcado no volviera a validar, el artefacto
    seria solo un registro y no una entrada reutilizable.
    """
    original = _descriptor()
    artifacts.persist_dataset_descriptor("tn_resend", original, DescriptorSource(format="json"))

    resent = DatasetDescriptor.model_validate(
        yaml.safe_load(_read("tn_resend", "dataset_descriptor.yaml"))
    )

    assert resent.model_dump() == original.model_dump()


def test_load_falls_back_to_legacy_json() -> None:
    """Las ejecuciones anteriores al cambio solo tienen el .json."""
    base_dir = artifacts._artifact_base_dir("tn_legacy")
    os.makedirs(base_dir, exist_ok=True)
    with open(os.path.join(base_dir, "dataset_descriptor.json"), "w", encoding="utf-8") as handle:
        json.dump(_descriptor().model_dump(), handle)

    assert artifacts.load_dataset_descriptor("tn_legacy").infrastructure.name == "tn_demo"


def test_load_raises_when_nothing_persisted() -> None:
    with pytest.raises(FileNotFoundError):
        artifacts.load_dataset_descriptor("tn_missing")


# --- peticion de experimento ---


def test_experiment_request_is_persisted_per_experiment() -> None:
    descriptor = _descriptor()

    path = artifacts.persist_experiment_request(
        "tn_exp", descriptor.experiment, descriptor.dataset, DescriptorSource(format="json")
    )

    assert path.endswith("experiment_request.yaml")
    assert os.path.exists(path)
    stored = yaml.safe_load(open(path, "r", encoding="utf-8").read())
    assert stored["experiment"]["name"] == "demo"
    assert stored["dataset"]["output"] == ["logs"]


def test_experiment_request_keeps_the_original_yaml() -> None:
    descriptor = _descriptor()
    raw = "experiment:\n  name: demo   # el de siempre\n  testcase_paths: [TC_ping.yml]\n"

    path = artifacts.persist_experiment_request(
        "tn_exp_raw",
        descriptor.experiment,
        descriptor.dataset,
        DescriptorSource(format="yaml", raw=raw),
    )

    assert open(path, "r", encoding="utf-8").read() == raw
