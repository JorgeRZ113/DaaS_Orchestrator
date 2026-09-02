"""Resolucion de las variables globales del bloque `dataset` en el orquestador.

Precedencia de cada variable: valor del body -> valor derivado del despliegue
(IP de monitorizacion del report TNLCM, Measurement del TestCase de captura) ->
default del overlay (que se consigue NO emitiendo la clave en los data values).
"""

from pathlib import Path

import pytest
import yaml

from app.services.phases import results
from app.storage import artifacts
from app.adapters import influx as influx_raw
from app.services.phases import elcm as elcm_phase

pytestmark = pytest.mark.usefixtures("isolate_artifacts_dir")

CAPTURE = {
    "Version": 2,
    "Name": "PrometheusToInflux_Capture",
    "Sequence": [
        {
            "Order": 3,
            "Task": "Flow.Parallel",
            "Children": [
                {
                    "Task": "Run.PrometheusToInflux",
                    "Config": {
                        "Measurement": "OPEN5GS_KPIS",
                        "QueriesRange": ["ues_active", "ran_ue"],
                    },
                }
            ],
        }
    ],
}


def _capture_file(tmp_path: Path) -> str:
    path = tmp_path / "TC_3_Prometheus_Capture_Open5GS.yml"
    path.write_text(yaml.safe_dump(CAPTURE), encoding="utf-8")
    return str(path)


async def _write_monitoring(execution_id: str, ip: str) -> None:
    await artifacts.build_tnlcm_summary_artifact(
        execution_id=execution_id,
        tn_id="tn-demo",
        report_summary={
            "monitoring": {
                "ip": ip,
                "credentials": {"token": "tok", "organization": "testing", "bucket": "testing"},
            }
        },
    )


# --- csv ---


@pytest.mark.asyncio
async def test_csv_derives_influx_host_from_the_tnlcm_report(tmp_path):
    # Sin variable en el body, la IP de InfluxDB es la de monitorizacion de ESTA
    # TN, no la IP de laboratorio que trae el overlay.
    await _write_monitoring("exec-a", "10.11.27.5")

    values = elcm_phase._dataset_data_values("csv", "exec-a", {}, [_capture_file(tmp_path)])

    assert values["dataset"]["influx"]["host"] == "10.11.27.5"
    # measurement se deriva del TestCase de captura.
    assert values["dataset"]["measurement"] == "OPEN5GS_KPIS"


@pytest.mark.asyncio
async def test_csv_body_variables_win_over_derived_values(tmp_path):
    await _write_monitoring("exec-b", "10.11.27.5")
    variables = {
        "influx_host": "192.168.50.9",
        "influx_port": 8087,
        "influx_bucket": "otro_bucket",
        "measurement": "MI_MEASUREMENT",
    }

    values = elcm_phase._dataset_data_values("csv", "exec-b", variables, [_capture_file(tmp_path)])

    assert values["dataset"]["influx"] == {"host": "192.168.50.9", "port": 8087}
    assert values["dataset"]["bucket"] == "otro_bucket"
    assert values["dataset"]["measurement"] == "MI_MEASUREMENT"


def test_csv_without_report_or_capture_falls_back_to_overlay_defaults():
    # Sin report ni captura no se emite ninguna clave: ytt usa los defaults del
    # overlay en vez de abortar.
    assert elcm_phase._dataset_data_values("csv", "exec-sin-nada", {}, []) is None


# --- dashboard ---


@pytest.mark.asyncio
async def test_dashboard_takes_metrics_from_the_capture_testcase(tmp_path):
    values = elcm_phase._dataset_data_values(
        "dashboard", "exec-c", {"panel_interval": "10s"}, [_capture_file(tmp_path)]
    )

    assert values["dataset"]["metrics"] == ["ues_active", "ran_ue"]
    assert values["dataset"]["measurement"] == "OPEN5GS_KPIS"
    assert values["dataset"]["interval"] == "10s"


def test_dashboard_without_capture_testcase_fails_fast():
    with pytest.raises(ValueError, match="requiere un TestCase de captura"):
        elcm_phase._dataset_data_values("dashboard", "exec-d", {}, [])


# --- raw ---


@pytest.mark.asyncio
async def test_raw_filters_by_the_requested_measurement(monkeypatch):
    await _write_monitoring("exec-e", "192.168.199.2")

    async def fake_collect(*, host, port, org, bucket, token, execution_id):
        assert bucket == "testing"
        return {"OPEN5GS_KPIS": "a,b\n1,2\n", "OTRO": "c,d\n3,4\n"}

    monkeypatch.setattr(influx_raw, "collect_raw_measurements", fake_collect)

    paths = await results.collect_raw("exec-e", "9", "exp", {"measurement": "OPEN5GS_KPIS"})

    assert [Path(p).name for p in paths] == ["raw_OPEN5GS_KPIS.csv"]


@pytest.mark.asyncio
async def test_raw_uses_the_bucket_from_the_body(monkeypatch):
    await _write_monitoring("exec-f", "192.168.199.2")
    seen: dict[str, str] = {}

    async def fake_collect(*, host, port, org, bucket, token, execution_id):
        seen["bucket"] = bucket
        return {"OPEN5GS_KPIS": "a,b\n1,2\n"}

    monkeypatch.setattr(influx_raw, "collect_raw_measurements", fake_collect)

    await results.collect_raw("exec-f", "9", "exp", {"influx_bucket": "mi_bucket"})

    assert seen["bucket"] == "mi_bucket"


@pytest.mark.asyncio
async def test_raw_fails_fast_when_the_requested_measurement_is_absent(monkeypatch):
    await _write_monitoring("exec-g", "192.168.199.2")

    async def fake_collect(*, host, port, org, bucket, token, execution_id):
        return {"OTRO": "c,d\n3,4\n"}

    monkeypatch.setattr(influx_raw, "collect_raw_measurements", fake_collect)

    with pytest.raises(ValueError, match="not found in InfluxDB"):
        await results.collect_raw("exec-g", "9", "exp", {"measurement": "NO_EXISTE"})
