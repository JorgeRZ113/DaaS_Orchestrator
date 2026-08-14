"""Entregas que se derivan del resumen del report TNLCM.

A diferencia de `csv`/`files`, estos dos recolectores no descargan nada de ELCM:
leen el resumen de la TN ya persistido y, a partir de el, construyen la URL del
dashboard de Grafana o consultan InfluxDB directamente. Comparten fichero porque
comparten esa fuente de datos, pero no se parametrizan juntos: sus resultados no
son comparables y forzar una tabla comun obligaria a ramificar dentro del test.
"""

import json
from pathlib import Path

import pytest

from app.adapters import influx as influx_raw
from app.services.phases import results
from app.storage import artifacts
from app.storage.artifacts import _artifact_result_dir

pytestmark = pytest.mark.usefixtures("isolate_artifacts_dir")


async def _write_summary(execution_id: str, monitoring: dict) -> None:
    """Deja persistido el resumen del report del que leen ambos recolectores."""
    await artifacts.build_tnlcm_summary_artifact(
        execution_id=execution_id,
        tn_id="tn-demo",
        report_summary={"monitoring": monitoring},
    )


# --- dashboard: URL de Grafana ------------------------------------------------


@pytest.mark.asyncio
async def test_collect_dashboard_results_writes_url():
    await _write_summary("exec-dash", {"ip": "192.168.199.2", "ports": [8086, 3000, 9090]})

    paths = await results.collect_dashboard("exec-dash", "9")

    assert len(paths) == 1
    dashboard_file = Path(paths[0])
    assert dashboard_file.parent.name == "result"

    payload = json.loads(dashboard_file.read_text(encoding="utf-8"))
    assert payload["url"] == "http://192.168.199.2:3000/d/Run9"
    assert payload["grafana_uid"] == "Run9"
    assert payload["elcm_execution_id"] == "9"


@pytest.mark.asyncio
async def test_collect_dashboard_results_raises_without_monitoring_ip():
    await _write_summary("exec-dash-noip", {"ip": None, "ports": [8086, 3000, 9090]})

    with pytest.raises(ValueError):
        await results.collect_dashboard("exec-dash-noip", "9")


# --- raw: un CSV por measurement de InfluxDB ----------------------------------


@pytest.mark.asyncio
async def test_collect_raw_results_writes_csv_per_measurement(monkeypatch):
    await _write_summary(
        "exec-raw",
        {
            "ip": "192.168.199.2",
            "ports": [8086, 3000, 9090],
            "credentials": {"token": "tok", "organization": "testing", "bucket": "testing"},
        },
    )

    async def fake_collect(*, host, port, org, bucket, token, execution_id):
        assert host == "192.168.199.2"
        assert port == 8086
        assert token == "tok"
        assert execution_id == "9"
        return {"OPEN5GS_KPIS": "a,b\n1,2\n", "node exporter": "c,d\n3,4\n"}

    monkeypatch.setattr(influx_raw, "collect_raw_measurements", fake_collect)

    paths = await results.collect_raw("exec-raw", "9", "exp-raw")

    # Los resultados se separan por experimento: result/<experimento>/.
    result_dir = Path(_artifact_result_dir("exec-raw", "exp-raw"))
    names = sorted(p.name for p in result_dir.iterdir())
    # El nombre del measurement se sanea (espacio -> _).
    assert names == ["raw_OPEN5GS_KPIS.csv", "raw_node_exporter.csv"]
    assert len(paths) == 2
    assert (result_dir / "raw_OPEN5GS_KPIS.csv").read_text(encoding="utf-8") == "a,b\n1,2\n"


@pytest.mark.asyncio
async def test_collect_raw_results_raises_without_token():
    await _write_summary("exec-raw-notoken", {"ip": "192.168.199.2", "credentials": {}})

    with pytest.raises(ValueError):
        await results.collect_raw("exec-raw-notoken", "9")
