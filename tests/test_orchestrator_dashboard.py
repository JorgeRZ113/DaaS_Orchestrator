import json
from pathlib import Path

import pytest

from app import artifacts, orchestrator
from app.config import settings


@pytest.fixture(autouse=True)
def _isolate_artifacts_dir(tmp_path):
    previous = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)
    yield
    settings.artifacts_dir = previous


async def _write_summary(execution_id: str, ip) -> None:
    await artifacts.build_tnlcm_summary_artifact(
        execution_id=execution_id,
        tn_id="tn-demo",
        report_summary={"monitoring": {"ip": ip, "ports": [8086, 3000, 9090]}},
    )


@pytest.mark.asyncio
async def test_collect_dashboard_results_writes_url():
    await _write_summary("exec-dash", "192.168.199.2")

    paths = await orchestrator._collect_dashboard_results("exec-dash", "9")

    assert len(paths) == 1
    dashboard_file = Path(paths[0])
    assert dashboard_file.parent.name == "result"

    payload = json.loads(dashboard_file.read_text(encoding="utf-8"))
    assert payload["url"] == "http://192.168.199.2:3000/d/Run9"
    assert payload["grafana_uid"] == "Run9"
    assert payload["elcm_execution_id"] == "9"


@pytest.mark.asyncio
async def test_collect_dashboard_results_raises_without_monitoring_ip():
    await _write_summary("exec-dash-noip", None)

    with pytest.raises(ValueError):
        await orchestrator._collect_dashboard_results("exec-dash-noip", "9")
