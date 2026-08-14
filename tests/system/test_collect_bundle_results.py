"""Entregas que llegan dentro del ZIP de resultados de ELCM.

`csv` y `files` son la misma operacion con distinto filtro: descargar el ZIP,
tirar los logs, descomprimir los ZIP internos y quedarse con unos ficheros u
otros. Por eso van parametrizadas juntas: anadir un filtro nuevo debe costar una
fila en `DELIVERIES`, no un fichero de pruebas nuevo.

Los recolectores se pasan como objeto funcion, no como cadena: si alguno se
renombra, esto falla al importar en vez de silenciarse.
"""

import zipfile
from pathlib import Path

import pytest

import app.adapters.elcm as elcm_module
from app.services.phases import results
from app.storage.artifacts import _artifact_result_dir

pytestmark = pytest.mark.usefixtures("isolate_artifacts_dir")

# (recolector, contenido del ZIP interno, ficheros que deben quedar)
DELIVERIES = [
    pytest.param(
        results.collect_csv,
        {"/csv_query_9.csv": "time,value\n1,2\n"},
        ["csv_query_9.csv"],
        id="csv",
    ),
    pytest.param(
        results.collect_files,
        {"result.csv": "a,b\n1,2\n", "report.txt": "hello\n"},
        ["report.txt", "result.csv"],
        id="files",
    ),
]

COLLECTORS = [pytest.param(d.values[0], id=d.id) for d in DELIVERIES]


def _fake_download_returning(inner_files: dict[str, str]):
    """Simula el ZIP que sirve ELCM: logs sueltos + un ZIP interno con los datos."""

    async def fake_download(experiment_id, dest_path, elcm_base_url=None, execution_id=None):
        inner = Path(dest_path).parent / "_inner_tmp.zip"
        with zipfile.ZipFile(inner, "w") as zf:
            for name, content in inner_files.items():
                zf.writestr(name, content)
        with zipfile.ZipFile(dest_path, "w") as zf:
            zf.writestr("Executor.log", "log\n")
            zf.write(inner, "dataset_9.zip")
        inner.unlink()
        return dest_path

    return fake_download


@pytest.mark.asyncio
@pytest.mark.parametrize("collector, inner_files, expected", DELIVERIES)
async def test_collect_extracts_only_the_data_files(monkeypatch, collector, inner_files, expected):
    monkeypatch.setattr(
        elcm_module, "download_execution_results", _fake_download_returning(inner_files)
    )

    produced = await collector("exec-1", "9", "http://elcm.local", "exp-1")

    # Los resultados se separan por experimento: result/<experimento>/.
    result_dir = Path(_artifact_result_dir("exec-1", "exp-1"))
    assert sorted(p.name for p in result_dir.iterdir()) == expected  # sin logs ni zips
    assert sorted(Path(p).name for p in produced) == expected
    assert all(Path(p).parent.name == "exp-1" for p in produced)


@pytest.mark.asyncio
@pytest.mark.parametrize("collector", COLLECTORS)
async def test_collect_returns_empty_when_elcm_has_no_results(monkeypatch, collector):
    async def fake_download(*args, **kwargs):
        raise elcm_module.ElcmResultsNotFoundError("no results")

    monkeypatch.setattr(elcm_module, "download_execution_results", fake_download)

    assert await collector("exec-none", "9", "http://elcm.local") == []
