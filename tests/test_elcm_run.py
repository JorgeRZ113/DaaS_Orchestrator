import json

import httpx
import pytest

from app import elcm
from app.models import ExperimentConfig

ELCM_BASE_URL = "http://elcm.local"


class _FakeAsyncClient:
    def __init__(self, response: httpx.Response):
        self._response = response
        self.post_calls: list[str] = []
        self.post_kwargs: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, **kwargs) -> httpx.Response:
        self.post_calls.append(url)
        self.post_kwargs.append(kwargs)
        return self._response


class _FakeGetClient:
    def __init__(self, response: httpx.Response):
        self._response = response
        self.get_calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, **kwargs) -> httpx.Response:
        self.get_calls.append(url)
        return self._response


def _response(status_code: int, body: dict[str, object] | str) -> httpx.Response:
    request = httpx.Request("POST", "http://elcm.local/elcm/api/v1/experiment/run")
    if isinstance(body, dict):
        return httpx.Response(status_code=status_code, json=body, request=request)
    return httpx.Response(status_code=status_code, text=body, request=request)


def _upload_response(status_code: int, body: dict[str, object] | str = "ok") -> httpx.Response:
    request = httpx.Request("POST", "http://elcm.local/elcm/api/v1/facility/upload_test_case")
    if isinstance(body, dict):
        return httpx.Response(status_code=status_code, json=body, request=request)
    return httpx.Response(status_code=status_code, text=body, request=request)


def _make_descriptor_file(tmp_path) -> str:
    descriptor_path = tmp_path / "Exp_Desc.json"
    descriptor_path.write_text(
        json.dumps({"Version": "1.0", "Application": "dummy"}), encoding="utf-8"
    )
    return str(descriptor_path)


@pytest.mark.asyncio
async def test_run_experiment_success_200_returns_execution_id(monkeypatch, tmp_path):
    descriptor_path = _make_descriptor_file(tmp_path)
    fake_client = _FakeAsyncClient(_response(200, {"ExecutionId": 321}))

    monkeypatch.setattr(elcm.httpx, "AsyncClient", lambda timeout=None: fake_client)

    execution_id = await elcm.run_experiment(
        ExperimentConfig(name="exp-ok"),
        elcm_base_url=ELCM_BASE_URL,
        exp_descriptor_path=descriptor_path,
    )

    assert execution_id == "321"
    assert len(fake_client.post_calls) == 1
    assert fake_client.post_calls[0].endswith("/elcm/api/v1/experiment/run")


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
    monkeypatch,
    tmp_path,
    backend_message,
):
    descriptor_path = _make_descriptor_file(tmp_path)
    fake_client = _FakeAsyncClient(_response(400, {"message": backend_message}))

    monkeypatch.setattr(elcm.httpx, "AsyncClient", lambda timeout=None: fake_client)

    with pytest.raises(RuntimeError) as exc_info:
        await elcm.run_experiment(
            ExperimentConfig(name="exp-fail"),
            elcm_base_url=ELCM_BASE_URL,
            exp_descriptor_path=descriptor_path,
        )

    message = str(exc_info.value)
    assert backend_message in message
    assert elcm.ELCM_RUN_ERROR_HINT in message
    assert len(fake_client.post_calls) == 1
    assert fake_client.post_calls[0].endswith("/elcm/api/v1/experiment/run")


@pytest.mark.asyncio
async def test_collect_results_logs_successfully_extracted_logs(monkeypatch, caplog):
    fake_client = _FakeGetClient(
        _response(200, {"logs": ["line-1", "line-2"], "metrics": {"count": 2}})
    )

    monkeypatch.setattr(elcm.httpx, "AsyncClient", lambda timeout=None: fake_client)
    caplog.set_level("INFO")

    result = await elcm.collect_results("exp-123", elcm_base_url=ELCM_BASE_URL)

    assert result["experiment_id"] == "exp-123"
    assert result["output"] == "logs"
    assert result["logs"]["metrics"]["count"] == 2
    assert "ELCM logs/metrics extracted successfully for experiment exp-123" in caplog.text
    assert len(fake_client.get_calls) == 1
    assert fake_client.get_calls[0].endswith("/elcm/api/v1/execution/exp-123/logs")


@pytest.mark.asyncio
async def test_collect_results_200_not_found_fails_without_retry(monkeypatch):
    fake_client = _FakeGetClient(_response(200, {"Status": "Not Found"}))

    monkeypatch.setattr(elcm.httpx, "AsyncClient", lambda timeout=None: fake_client)

    with pytest.raises(elcm.TnLogsNotFoundError) as exc_info:
        await elcm.collect_results("exp-404", elcm_base_url=ELCM_BASE_URL)

    message = str(exc_info.value)
    assert "exp-404" in message
    assert "hay que repetirlo" in message
    assert len(fake_client.get_calls) == 1
    assert fake_client.get_calls[0].endswith("/elcm/api/v1/execution/exp-404/logs")


@pytest.mark.asyncio
async def test_upload_test_cases_logs_success_on_200(monkeypatch, tmp_path, caplog):
    testcase_path = tmp_path / "TC_ping.yml"
    testcase_path.write_text("name: ping", encoding="utf-8")
    fake_client = _FakeAsyncClient(_upload_response(200, "uploaded"))

    monkeypatch.setattr(elcm.httpx, "AsyncClient", lambda timeout=None: fake_client)
    monkeypatch.setattr(elcm, "_resolve_examples_path", lambda _: str(testcase_path))
    caplog.set_level("INFO")

    await elcm.upload_test_cases(["TC_ping.yml"], elcm_base_url=ELCM_BASE_URL)

    assert len(fake_client.post_calls) == 1
    assert fake_client.post_calls[0].endswith("/elcm/api/v1/facility/upload_test_case")
    assert "ELCM testcase uploaded successfully: TC_ping.yml" in caplog.text
    # Por defecto se sube como TestCase.
    assert fake_client.post_kwargs[0]["files"]["file_type"] == (None, "testcase")


@pytest.mark.asyncio
async def test_upload_test_cases_sends_ues_file_type(monkeypatch, tmp_path):
    # Los UEs tienen que subirse con file_type="ues": con "testcase" acabarian en
    # la carpeta equivocada de ELCM y Facility no los registraria nunca.
    ue_path = tmp_path / "UE_Variables.yml"
    ue_path.write_text("UE_Variables:\n  - Order: 0\n    Task: Run.Publish\n", encoding="utf-8")
    fake_client = _FakeAsyncClient(_upload_response(200, "uploaded"))

    monkeypatch.setattr(elcm.httpx, "AsyncClient", lambda timeout=None: fake_client)

    await elcm.upload_test_cases([str(ue_path)], elcm_base_url=ELCM_BASE_URL, file_type="ues")

    assert len(fake_client.post_calls) == 1
    files = fake_client.post_kwargs[0]["files"]
    assert files["file_type"] == (None, "ues")
    assert files["test_case"][0] == "UE_Variables.yml"


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
    monkeypatch,
    tmp_path,
    status_code,
    backend_message,
):
    testcase_path = tmp_path / "TC_ping.yml"
    testcase_path.write_text("name: ping", encoding="utf-8")
    fake_client = _FakeAsyncClient(_upload_response(status_code, {"message": backend_message}))

    monkeypatch.setattr(elcm.httpx, "AsyncClient", lambda timeout=None: fake_client)
    monkeypatch.setattr(elcm, "_resolve_examples_path", lambda _: str(testcase_path))

    with pytest.raises(elcm.TnUploadTestCaseError) as exc_info:
        await elcm.upload_test_cases(["TC_ping.yml"], elcm_base_url=ELCM_BASE_URL)

    message = str(exc_info.value)
    assert backend_message in message
    assert elcm.ELCM_UPLOAD_ERROR_HINT in message
    assert len(fake_client.post_calls) == 1
    assert fake_client.post_calls[0].endswith("/elcm/api/v1/facility/upload_test_case")


@pytest.mark.asyncio
async def test_download_execution_results_writes_zip(monkeypatch, tmp_path):
    zip_bytes = b"PK\x03\x04fake-zip-content"
    request = httpx.Request("GET", "http://elcm.local/elcm/api/v1/execution/9/results")
    fake_client = _FakeGetClient(httpx.Response(200, content=zip_bytes, request=request))

    monkeypatch.setattr(elcm.httpx, "AsyncClient", lambda timeout=None: fake_client)

    dest = tmp_path / "result" / "csv_results_9.zip"
    path = await elcm.download_execution_results(
        "9", dest_path=str(dest), elcm_base_url=ELCM_BASE_URL
    )

    assert path == str(dest)
    assert dest.read_bytes() == zip_bytes
    assert len(fake_client.get_calls) == 1
    assert fake_client.get_calls[0].endswith("/elcm/api/v1/execution/9/results")


@pytest.mark.asyncio
async def test_download_execution_results_404_raises_not_found(monkeypatch, tmp_path):
    request = httpx.Request("GET", "http://elcm.local/elcm/api/v1/execution/9/results")
    fake_client = _FakeGetClient(
        httpx.Response(404, text="No results for execution 9", request=request)
    )

    monkeypatch.setattr(elcm.httpx, "AsyncClient", lambda timeout=None: fake_client)

    dest = tmp_path / "result" / "csv_results_9.zip"
    with pytest.raises(elcm.ElcmResultsNotFoundError):
        await elcm.download_execution_results("9", dest_path=str(dest), elcm_base_url=ELCM_BASE_URL)

    assert not dest.exists()
