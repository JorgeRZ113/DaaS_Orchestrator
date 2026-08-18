"""Lectura y validacion de los artefactos que se suben a ELCM.

Dos familias con el mismo proposito: comprobar los ficheros ANTES de subirlos,
para que un error de sintaxis falle aqui y no a mitad del experimento, con la
infraestructura ya desplegada y el nombre del experimento quemado.

* TestCases de captura -> de donde salen el `Measurement` y las metricas que
  alimentan al generador de datasets.
* Ficheros UE -> un UE no es un TestCase V2: es una lista de acciones estilo V1
  cuya clave raiz es el nombre con el que ELCM lo registra.
"""

from pathlib import Path

import pytest

from app.adapters import elcm
from app.rendering.paths import elcm_testcase_dir

CAPTURE_YAML = """
Version: 2
Name: "PrometheusToInflux_Capture"
Sequence:
  - Order: 1
    Task: "Flow.Parallel"
    Children:
      - Task: "Run.PrometheusToInflux"
        Config:
          Measurement: "OPEN5GS_GRAFANA"
          QueriesRange:
            - "ues_active"
            - "ran_ue"
            - '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[30s])) * 100)'
      - Task: "Flow.Sequence"
        Children:
          - Task: "Run.Delay"
            Config:
              Time: 120
"""

PING_YAML = """
Version: 2
Name: "Test_ping"
Sequence:
  - Order: 1
    Task: "Run.CliExecute"
    Config:
      Command: "ping -c 1 8.8.8.8"
"""

UE_VALID = (
    "UE_Variables:\n  - Order: 0\n    Task: Run.Publish\n    Config:\n      SutIp: '10.0.0.1'\n"
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# --- TestCases de captura: measurement y metricas -----------------------------


def test_extract_capture_metrics_reads_measurement_and_simple_metrics(tmp_path):
    ping = _write(tmp_path, "TC_ping.yml", PING_YAML)
    capture = _write(tmp_path, "TestCase_prometheus_capture2.yml", CAPTURE_YAML)

    result = elcm.extract_capture_metrics([str(ping), str(capture)])

    assert result is not None
    measurement, metrics = result
    assert measurement == "OPEN5GS_GRAFANA"
    # La query con agregacion (parentesis/comillas) se descarta: solo nombres simples.
    assert metrics == ["ues_active", "ran_ue"]


def test_extract_capture_metrics_ignores_files_without_capture_in_name(tmp_path):
    # Tiene Run.PrometheusToInflux pero su nombre NO contiene "_capture".
    other = _write(tmp_path, "TestCase_prometheus.yml", CAPTURE_YAML)

    assert elcm.extract_capture_metrics([str(other)]) is None


def test_extract_capture_metrics_none_when_no_capture(tmp_path):
    ping = _write(tmp_path, "TC_ping.yml", PING_YAML)

    assert elcm.extract_capture_metrics([str(ping)]) is None


# --- Ficheros UE: resolucion y forma ------------------------------------------


def test_extract_ue_name_returns_root_key(tmp_path):
    path = _write(tmp_path, "UE_Variables_TEMPLATE.yml", UE_VALID)

    # El nombre es la clave raiz, no el stem del fichero.
    assert elcm.extract_ue_name(str(path)) == "UE_Variables"


def test_resolve_ue_file_accepts_absolute_path(tmp_path):
    path = _write(tmp_path, "ue.yml", UE_VALID)

    assert elcm.resolve_ue_file(str(path)) == path.resolve()


def test_resolve_ue_file_falls_back_to_testcase_library(monkeypatch, tmp_path):
    path = _write(tmp_path, "ue.yml", UE_VALID)
    monkeypatch.setattr(elcm, "elcm_testcase_dir", lambda: tmp_path)

    assert elcm.resolve_ue_file("ue.yml") == path.resolve()


def test_resolve_ue_file_looks_up_the_library_by_file_name(monkeypatch, tmp_path):
    # La biblioteca es plana: una referencia con directorios delante (o con '..')
    # se busca por su nombre de fichero, sin salirse de templates/ELCM/TestCase/.
    path = _write(tmp_path, "ue.yml", UE_VALID)
    monkeypatch.setattr(elcm, "elcm_testcase_dir", lambda: tmp_path)

    assert elcm.resolve_ue_file("../../otro_sitio/ue.yml") == path.resolve()


def test_resolve_ue_file_fails_fast_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(elcm, "elcm_testcase_dir", lambda: tmp_path)

    with pytest.raises(FileNotFoundError, match="UE file not found"):
        elcm.resolve_ue_file("no_existe.yml")


def test_extract_ue_name_rejects_testcase_syntax(tmp_path):
    # El endpoint de subida de ELCM rechaza 'Name' sin 'Version: 2', y UeLoader
    # tomaria cada clave raiz por un UE distinto. Se detecta antes de subir.
    path = _write(tmp_path, "malo.yml", "Version: 2\nName: TC_Algo\nSequence: []\n")

    with pytest.raises(ValueError, match="TestCase V2 syntax"):
        elcm.extract_ue_name(str(path))


def test_extract_ue_name_rejects_multiple_root_keys(tmp_path):
    path = _write(tmp_path, "dos.yml", UE_VALID + "OtroUE:\n  - Order: 1\n    Task: Run.Dummy\n")

    with pytest.raises(ValueError, match="multiple root keys"):
        elcm.extract_ue_name(str(path))


def test_extract_ue_name_rejects_action_without_order(tmp_path):
    # 'Order' es obligatorio en toda accion de primer nivel (ActionInformation).
    path = _write(tmp_path, "sin_order.yml", "UE_Variables:\n  - Task: Run.Publish\n")

    with pytest.raises(ValueError, match="no 'Order'"):
        elcm.extract_ue_name(str(path))


def test_extract_ue_name_rejects_non_list_body(tmp_path):
    path = _write(tmp_path, "raro.yml", "UE_Variables:\n  Task: Run.Publish\n")

    with pytest.raises(ValueError, match="non-empty action list"):
        elcm.extract_ue_name(str(path))


def test_extract_ue_name_rejects_empty_file(tmp_path):
    path = _write(tmp_path, "vacio.yml", "")

    with pytest.raises(ValueError, match="single root key"):
        elcm.extract_ue_name(str(path))


def test_shipped_ue_template_is_valid():
    """La plantilla que se entrega al experimentador tiene que pasar la validacion."""
    template = elcm_testcase_dir() / "UE_Variables_TEMPLATE.yml"

    assert elcm.extract_ue_name(str(template)) == "UE_Variables"
