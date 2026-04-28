import logging

import httpx
import pytest

from app import tnlcm


class _FakeClient:
    def __init__(self, response: httpx.Response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, *args, **kwargs) -> httpx.Response:
        return self._response


def _response(status_code: int, body: str = "") -> httpx.Response:
    request = httpx.Request(
        "GET", "http://tnlcm.local/api/v1/trial-networks/tn-demo/report/download"
    )
    return httpx.Response(status_code=status_code, text=body, request=request)


def test_download_trial_network_report_success_logs_ready(monkeypatch, caplog):
    monkeypatch.setattr(
        tnlcm.httpx, "Client", lambda timeout: _FakeClient(_response(200, "# report"))
    )

    caplog.set_level(logging.INFO)
    report = tnlcm.download_trial_network_report("tn-demo")

    assert report == "# report"
    assert "TNLCM report ready for tn_id=tn-demo" in caplog.text


@pytest.mark.parametrize(
    ("status_code", "expected_exception"),
    [
        (404, tnlcm.TnNotFoundError),
        (400, tnlcm.TnNotActivatedError),
        (500, tnlcm.TnReportGenerationError),
    ],
)
def test_download_trial_network_report_maps_expected_http_errors(
    monkeypatch,
    status_code,
    expected_exception,
):
    monkeypatch.setattr(
        tnlcm.httpx, "Client", lambda timeout: _FakeClient(_response(status_code, "err"))
    )

    with pytest.raises(expected_exception):
        tnlcm.download_trial_network_report("tn-demo")
