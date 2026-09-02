"""Contrato HTTP de la descarga del report de una Trial Network.

El codigo de estado que devuelve TNLCM decide la excepcion, porque cada uno pide
una reaccion distinta arriba: la TN no existe, aun no esta activada, o TNLCM no
ha podido generar el report.
"""

import logging

import pytest

from app.adapters import tnlcm

REPORT_PATH = "/api/v1/trial-networks/tn-demo/report/download"


def test_download_trial_network_report_returns_markdown(fake_http, tnlcm_token, caplog):
    fake_http.respond(200, text="# report")
    caplog.set_level(logging.INFO)

    report = tnlcm.download_trial_network_report("tn-demo")

    assert report == "# report"
    assert "TNLCM report ready for tn_id=tn-demo" in caplog.text
    assert fake_http.paths == [REPORT_PATH]
    assert fake_http.last.headers["Authorization"] == f"Bearer {tnlcm_token}"


@pytest.mark.parametrize(
    ("status_code", "expected_exception"),
    [
        (404, tnlcm.TnNotFoundError),
        (400, tnlcm.TnNotActivatedError),
        (500, tnlcm.TnReportGenerationError),
    ],
)
def test_download_trial_network_report_maps_expected_http_errors(
    fake_http, tnlcm_token, status_code, expected_exception
):
    fake_http.respond(status_code, text="err")

    with pytest.raises(expected_exception):
        tnlcm.download_trial_network_report("tn-demo")

    assert fake_http.paths == [REPORT_PATH]
