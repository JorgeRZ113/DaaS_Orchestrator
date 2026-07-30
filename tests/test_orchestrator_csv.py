import zipfile
from pathlib import Path

import pytest

import app.elcm as elcm_module
from app import orchestrator
from app.artifacts import _artifact_result_dir
from app.config import settings


@pytest.fixture(autouse=True)
def _isolate_artifacts_dir(tmp_path):
    previous = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)
    yield
    settings.artifacts_dir = previous


@pytest.mark.asyncio
async def test_collect_csv_results_downloads_and_extracts(monkeypatch):
    async def fake_download(experiment_id, dest_path, elcm_base_url=None, execution_id=None):
        # Simula el ZIP de ELCM: logs + ZIP interno con el CSV.
        inner = Path(dest_path).parent / "_inner_tmp.zip"
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("/csv_query_9.csv", "time,value\n1,2\n")
        with zipfile.ZipFile(dest_path, "w") as zf:
            zf.writestr("Executor.log", "log\n")
            zf.write(inner, "dataset_9.zip")
        inner.unlink()
        return dest_path

    monkeypatch.setattr(elcm_module, "download_execution_results", fake_download)

    csv_files = await orchestrator._collect_csv_results(
        "exec-csv", "9", "http://elcm.local", "exp-csv"
    )

    # Los resultados se separan por experimento: result/<experimento>/.
    result_dir = Path(_artifact_result_dir("exec-csv", "exp-csv"))
    names = sorted(p.name for p in result_dir.iterdir())
    assert names == ["csv_query_9.csv"]  # sin logs ni zips
    assert [Path(p).name for p in csv_files] == ["csv_query_9.csv"]
    assert all(Path(p).parent.name == "exp-csv" for p in csv_files)


@pytest.mark.asyncio
async def test_collect_csv_results_returns_empty_when_no_results(monkeypatch):
    async def fake_download(*args, **kwargs):
        raise elcm_module.ElcmResultsNotFoundError("no results")

    monkeypatch.setattr(elcm_module, "download_execution_results", fake_download)

    csv_files = await orchestrator._collect_csv_results("exec-none", "9", "http://elcm.local")

    assert csv_files == []
