"""Contrato HTTP del adaptador de ELCM: lanzar, consultar, subir y descargar.

Las pruebas no sustituyen el cliente httpx sino su transporte (`fake_http`), asi
que lo que se afirma es lo que ELCM recibiria de verdad: metodo, ruta y cuerpo.
"""

import json

import pytest

from app.adapters import elcm
from app.domain.descriptor import ExperimentConfig

ELCM_BASE_URL = "http://elcm.local"

RUN_PATH = "/elcm/api/v1/experiment/run"
UPLOAD_PATH = "/elcm/api/v1/facility/upload_test_case"


def _make_descriptor_file(tmp_path) -> str:
    descriptor_path = tmp_path / "Exp_Desc.json"
    descriptor_path.write_text(
        json.dumps({"Version": "1.0", "Application": "dummy"}), encoding="utf-8"
    )
    return str(descriptor_path)


# --- POST /experiment/run -----------------------------------------------------


@pytest.mark.asyncio
async def test_run_experiment_success_200_returns_execution_id(fake_http, tmp_path):
    fake_http.respond(200, json={"ExecutionId": 321})

    execution_id = await elcm.run_experiment(
        ExperimentConfig(name="exp-ok"),
        elcm_base_url=ELCM_BASE_URL,
        exp_descriptor_path=_make_descriptor_file(tmp_path),
    )

    assert execution_id == "321"
    assert fake_http.paths == [RUN_PATH]
    # El nombre del experimento se inyecta en el descriptor antes de enviarlo.
    assert json.loads(fake_http.last.content)["Application"] == "exp-ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend_message",
    [
        "payload vacio o nulo",
        "descriptor invalido: faltan claves obligatorias",
        "excepcion durante la creacion: UserId invalido",
    ],
)
async def test_run_experiment_400_does_not_retry_and_includes_backend_error_hint(
    fake_http, tmp_path, backend_message
):
    fake_http.respond(400, json={"message": backend_message})

    with pytest.raises(RuntimeError) as exc_info:
        await elcm.run_experiment(
            ExperimentConfig(name="exp-fail"),
            elcm_base_url=ELCM_BASE_URL,
            exp_descriptor_path=_make_descriptor_file(tmp_path),
        )

    message = str(exc_info.value)
    assert backend_message in message
    assert elcm.ELCM_RUN_ERROR_HINT in message
    # Un 400 es descriptor mal formado: reintentarlo solo retrasaria el fallo.
    assert fake_http.paths == [RUN_PATH]


@pytest.mark.asyncio
async def test_run_experiment_retries_5xx_before_giving_up(fake_http, tmp_path, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    fake_http.respond_once(503, text="service unavailable")
    fake_http.respond(200, json={"ExecutionId": 77})

    execution_id = await elcm.run_experiment(
        ExperimentConfig(name="exp-flaky"),
        elcm_base_url=ELCM_BASE_URL,
        exp_descriptor_path=_make_descriptor_file(tmp_path),
    )

    assert execution_id == "77"
    assert fake_http.paths == [RUN_PATH, RUN_PATH]  # un reintento, no mas


async def _no_sleep(_seconds) -> None:
    """Evita la espera real del backoff sin tocar la politica de reintentos."""


# --- GET /execution/{id}/logs -------------------------------------------------


@pytest.mark.asyncio
async def test_collect_results_logs_successfully_extracted_logs(fake_http, caplog):
    fake_http.respond(200, json={"logs": ["line-1", "line-2"], "metrics": {"count": 2}})
    caplog.set_level("INFO")

    result = await elcm.collect_results("exp-123", elcm_base_url=ELCM_BASE_URL)

    assert result["experiment_id"] == "exp-123"
    assert result["output"] == "logs"
    assert result["logs"]["metrics"]["count"] == 2
    assert "ELCM logs/metrics extracted successfully for experiment exp-123" in caplog.text
    assert fake_http.paths == ["/elcm/api/v1/execution/exp-123/logs"]


@pytest.mark.asyncio
async def test_collect_results_200_not_found_fails_without_retry(fake_http):
    # ELCM responde 200 con cuerpo "Not Found": el codigo HTTP no basta.
    fake_http.respond(200, json={"Status": "Not Found"})

    with pytest.raises(elcm.TnLogsNotFoundError) as exc_info:
        await elcm.collect_results("exp-404", elcm_base_url=ELCM_BASE_URL)

    message = str(exc_info.value)
    assert "exp-404" in message
    assert "hay que repetirlo" in message
    assert fake_http.paths == ["/elcm/api/v1/execution/exp-404/logs"]


# --- POST /facility/upload_test_case ------------------------------------------


@pytest.mark.asyncio
async def test_upload_test_cases_sends_testcase_file_type_by_default(
    fake_http, tmp_path, monkeypatch, caplog
):
    testcase_path = tmp_path / "TC_ping.yml"
    testcase_path.write_text("name: ping", encoding="utf-8")
    monkeypatch.setattr(elcm, "elcm_testcase_dir", lambda: tmp_path)
    fake_http.respond(200, text="uploaded")
    caplog.set_level("INFO")

    await elcm.upload_test_cases(["TC_ping.yml"], elcm_base_url=ELCM_BASE_URL)

    assert fake_http.paths == [UPLOAD_PATH]
    assert "ELCM testcase uploaded successfully: TC_ping.yml" in caplog.text

    fields = fake_http.multipart()
    assert fields["file_type"][1] == "testcase"
    assert fields["test_case"][0] == "TC_ping.yml"


@pytest.mark.asyncio
async def test_upload_test_cases_sends_ues_file_type(fake_http, tmp_path):
    # Los UEs tienen que subirse con file_type="ues": con "testcase" acabarian en
    # la carpeta equivocada de ELCM y Facility no los registraria nunca.
    ue_path = tmp_path / "UE_Variables.yml"
    ue_path.write_text("UE_Variables:\n  - Order: 0\n    Task: Run.Publish\n", encoding="utf-8")
    fake_http.respond(200, text="uploaded")

    await elcm.upload_test_cases([str(ue_path)], elcm_base_url=ELCM_BASE_URL, file_type="ues")

    assert fake_http.paths == [UPLOAD_PATH]
    fields = fake_http.multipart()
    assert fields["file_type"][1] == "ues"
    assert fields["test_case"][0] == "UE_Variables.yml"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "backend_message"),
    [
        (400, "user_id ausente/invalido"),
        (400, "archivo ausente o extension distinta de .yml"),
        (400, "YAML invalido"),
        (400, "formato invalido (Name y Version: 2 no coherentes)"),
        (500, "error guardando archivo"),
    ],
)
async def test_upload_test_cases_fails_without_retry_and_reports_backend_error(
    fake_http, tmp_path, monkeypatch, status_code, backend_message
):
    testcase_path = tmp_path / "TC_ping.yml"
    testcase_path.write_text("name: ping", encoding="utf-8")
    monkeypatch.setattr(elcm, "elcm_testcase_dir", lambda: tmp_path)
    fake_http.respond(status_code, json={"message": backend_message})

    with pytest.raises(elcm.TnUploadTestCaseError) as exc_info:
        await elcm.upload_test_cases(["TC_ping.yml"], elcm_base_url=ELCM_BASE_URL)

    message = str(exc_info.value)
    assert backend_message in message
    assert elcm.ELCM_UPLOAD_ERROR_HINT in message
    assert fake_http.paths == [UPLOAD_PATH]


# --- GET /execution/{id}/results ----------------------------------------------


@pytest.mark.asyncio
async def test_download_execution_results_writes_zip(fake_http, tmp_path):
    zip_bytes = b"PK\x03\x04fake-zip-content"
    fake_http.respond(200, content=zip_bytes)

    dest = tmp_path / "result" / "csv_results_9.zip"
    path = await elcm.download_execution_results(
        "9", dest_path=str(dest), elcm_base_url=ELCM_BASE_URL
    )

    assert path == str(dest)
    assert dest.read_bytes() == zip_bytes
    assert fake_http.paths == ["/elcm/api/v1/execution/9/results"]


@pytest.mark.asyncio
async def test_download_execution_results_404_raises_not_found(fake_http, tmp_path):
    fake_http.respond(404, text="No results for execution 9")

    dest = tmp_path / "result" / "csv_results_9.zip"
    with pytest.raises(elcm.ElcmResultsNotFoundError):
        await elcm.download_execution_results("9", dest_path=str(dest), elcm_base_url=ELCM_BASE_URL)

    assert not dest.exists()
