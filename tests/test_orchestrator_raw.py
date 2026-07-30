from pathlib import Path

import pytest

from app import artifacts, orchestrator
from app.artifacts import _artifact_result_dir
from app.config import settings
from app.utils import influx_raw


@pytest.fixture(autouse=True)
def _isolate_artifacts_dir(tmp_path):
    previous = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)
    yield
    settings.artifacts_dir = previous


async def _write_summary(execution_id: str, monitoring: dict) -> None:
    await artifacts.build_tnlcm_summary_artifact(
        execution_id=execution_id,
        tn_id="tn-demo",
        report_summary={"monitoring": monitoring},
    )


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

    paths = await orchestrator._collect_raw_results("exec-raw", "9", "exp-raw")

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
        await orchestrator._collect_raw_results("exec-raw-notoken", "9")
