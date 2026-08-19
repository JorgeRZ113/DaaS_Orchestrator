"""GET /executions/{id}/download: que empaqueta, que deja fuera y que rechaza.

Las dos cosas que de verdad importan aqui no son el formato del ZIP sino:

- que los ficheros con claves de acceso NO salgan si no se piden. La carpeta de
  artefactos guarda secretos en claro (deuda §8.7): la clave privada del tunel
  WireGuard y el token de InfluxDB. Un ZIP que los arrastre sin avisar convierte
  «me descargo los resultados» en una fuga.
- que `execution_id` no pueda salirse de `artifacts/`. Es la primera ruta de
  LECTURA que acepta el identificador tal cual, y el identificador viene de
  `infrastructure.name`, que es entrada de usuario sin validar (deuda §8.3).
"""

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord, ExperimentRun
from app.main import app
from app.services import state
from app.storage import artifacts

client = TestClient(app)

pytestmark = pytest.mark.usefixtures("isolate_artifacts_dir", "isolate_orchestrator_state")

EXECUTION_ID = "tn-descarga"

# Lo que la carpeta de una ejecucion real contiene, con un fichero por familia.
ARTIFACT_TREE = {
    "dataset_descriptor.yaml": "infrastructure:\n  name: tn-descarga\n",
    "summary.md": "# Execution summary\n",
    "summary.json": '{"execution_id": "tn-descarga"}',
    "telemetry.log": '{"event": "phase"}\n',
    "telemetry_report_tnlcm_completed.json": "{}",
    "archivos_generados/base_overlay_filled.yaml": "influxdb_password: secreta\n",
    "result/exp-demo/logs.json": "[]",
    "result/exp-demo/metadata.json": "{}",
    # Los tres con claves de acceso.
    f"{EXECUTION_ID}.conf": "[Interface]\nPrivateKey = CLAVE_PRIVADA\n",
    "tnlcm_report_raw.md": "token: SECRETO\n",
    "tnlcm_report_summary.json": '{"credentials": {"token": "SECRETO"}}',
}

SECRET_ENTRIES = {
    f"{EXECUTION_ID}.conf",
    "tnlcm_report_raw.md",
    "tnlcm_report_summary.json",
}


def _headers() -> dict[str, str]:
    return {"x-api-key": settings.api_key}


@pytest.fixture
def execution_dir(isolate_artifacts_dir):
    """Deja en disco una carpeta de artefactos con la forma de una real."""
    from pathlib import Path

    base = Path(artifacts.artifact_root_dir()) / EXECUTION_ID
    for relative, content in ARTIFACT_TREE.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return base


@pytest.fixture
def recorded_execution():
    """El record en memoria, sin el cual no se puede generar el README."""
    record = ExecutionRecord(
        execution_id=EXECUTION_ID,
        status=ExecutionState.destroyed,
        tn_id=EXECUTION_ID,
        dataset_output=["logs", "csv"],
        experiments=[ExperimentRun(name="exp-demo", status="FINISHED")],
    )
    state.executions[EXECUTION_ID] = record
    return record


def _entries(response) -> set[str]:
    assert response.status_code == 200, response.text
    return set(zipfile.ZipFile(io.BytesIO(response.content)).namelist())


def test_bundle_omits_files_holding_access_keys(execution_dir, recorded_execution) -> None:
    """Lo que no se pide explicitamente, no viaja."""
    entries = _entries(client.get(f"/executions/{EXECUTION_ID}/download", headers=_headers()))

    assert entries.isdisjoint(SECRET_ENTRIES)
    # Y lo demas si esta: excluir de mas dejaria el ZIP inservible.
    assert "dataset_descriptor.yaml" in entries
    assert "result/exp-demo/logs.json" in entries
    assert "archivos_generados/base_overlay_filled.yaml" in entries


def test_bundle_includes_them_when_explicitly_requested(execution_dir, recorded_execution) -> None:
    entries = _entries(
        client.get(f"/executions/{EXECUTION_ID}/download?secrets=true", headers=_headers())
    )

    assert SECRET_ENTRIES.issubset(entries)


def test_bundle_is_a_zip_with_relative_paths(execution_dir, recorded_execution) -> None:
    """Al descomprimir no debe recrearse el arbol `artifacts/<id>/` entero."""
    response = client.get(f"/executions/{EXECUTION_ID}/download", headers=_headers())

    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == f'attachment; filename="{EXECUTION_ID}.zip"'
    for name in _entries(response):
        assert not name.startswith("/")
        assert ".." not in name
        assert EXECUTION_ID not in name.split("/")[0] or name.startswith(EXECUTION_ID)


def test_bundle_carries_a_readme_with_the_execution_metadata(
    execution_dir, recorded_execution
) -> None:
    """Es lo que pide F6.2: que el ZIP se entienda sin volver al orquestador."""
    response = client.get(f"/executions/{EXECUTION_ID}/download", headers=_headers())
    archive = zipfile.ZipFile(io.BytesIO(response.content))

    readme = archive.read("README.md").decode("utf-8")

    assert EXECUTION_ID in readme
    assert "exp-demo" in readme
    assert "dataset_descriptor.yaml" in readme


def test_bundle_works_without_the_record_in_memory(execution_dir) -> None:
    """Una carpeta huerfana (proceso reiniciado) se empaqueta igual, sin README."""
    entries = _entries(client.get(f"/executions/{EXECUTION_ID}/download", headers=_headers()))

    assert "README.md" not in entries
    assert "dataset_descriptor.yaml" in entries


def test_unknown_execution_returns_404(isolate_artifacts_dir) -> None:
    response = client.get("/executions/no-existe/download", headers=_headers())

    assert response.status_code == 404


@pytest.mark.parametrize(
    "execution_id",
    [
        "tn con espacios",
        "tn;rm",
        "tn.deveop",  # el punto abre la puerta a `..`
        "tn/otro",
        "a" * 65,
    ],
)
def test_identifiers_that_could_escape_the_root_are_rejected(
    isolate_artifacts_dir, execution_id: str
) -> None:
    """Ningun identificador fuera de ^[A-Za-z0-9_-]{1,64}$ llega al filesystem."""
    response = client.get(f"/executions/{execution_id}/download", headers=_headers())

    assert response.status_code in {400, 404}
    if response.status_code == 400:
        assert "execution_id" in response.json()["detail"]


def test_the_guard_rejects_what_no_http_client_would_even_send() -> None:
    """Casos que no llegan por HTTP pero que la funcion no puede dar por imposibles.

    El byte nulo lo corta httpx antes de enviar y `..` lo normaliza el enrutador,
    asi que por la via HTTP nunca alcanzan el handler. La validacion es del
    modulo, no del transporte: si manana se llama desde un script o un test, la
    garantia tiene que seguir en pie.
    """
    from app.storage.execution_bundle import InvalidExecutionIdError, validate_execution_id

    for identifier in ("tn\x00", "../../etc", "..", "", "/etc/passwd", "C:\\Windows"):
        with pytest.raises(InvalidExecutionIdError):
            validate_execution_id(identifier)


def test_a_symlink_out_of_the_root_is_not_followed(isolate_artifacts_dir, tmp_path) -> None:
    """La contencion se comprueba sobre la ruta resuelta, no sobre el texto."""
    from pathlib import Path

    outside = tmp_path / "fuera"
    outside.mkdir()
    (outside / "secreto.txt").write_text("no deberia salir", encoding="utf-8")

    link = Path(artifacts.artifact_root_dir()) / "enlace"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("crear enlaces simbolicos requiere privilegios en Windows")

    response = client.get("/executions/enlace/download", headers=_headers())

    assert response.status_code == 400
