from app import elcm

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


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_extract_capture_metrics_reads_measurement_and_simple_metrics(tmp_path):
    ping = _write(tmp_path, "TC_ping.yml", PING_YAML)
    capture = _write(tmp_path, "TestCase_prometheus_capture2.yml", CAPTURE_YAML)

    result = elcm.extract_capture_metrics([ping, capture])

    assert result is not None
    measurement, metrics = result
    assert measurement == "OPEN5GS_GRAFANA"
    # La query con agregacion (parentesis/comillas) se descarta: solo nombres simples.
    assert metrics == ["ues_active", "ran_ue"]


def test_extract_capture_metrics_ignores_files_without_capture_in_name(tmp_path):
    # Tiene Run.PrometheusToInflux pero su nombre NO contiene "_capture".
    other = _write(tmp_path, "TestCase_prometheus.yml", CAPTURE_YAML)

    assert elcm.extract_capture_metrics([other]) is None


def test_extract_capture_metrics_none_when_no_capture(tmp_path):
    ping = _write(tmp_path, "TC_ping.yml", PING_YAML)

    assert elcm.extract_capture_metrics([ping]) is None
