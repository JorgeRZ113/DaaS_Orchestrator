"""Contrato HTTP de la consulta de estado de una Trial Network."""

import pytest

from app.adapters import tnlcm

STATUS_PATH = "/api/v1/trial-networks/tn-demo"


def test_get_tn_status_returns_the_status_field(fake_http, tnlcm_token):
    fake_http.respond(200, text='{"status": "ACTIVE"}')

    assert tnlcm.get_tn_status("tn-demo") == "ACTIVE"

    assert fake_http.paths == [STATUS_PATH]
    assert fake_http.last.headers["Authorization"] == f"Bearer {tnlcm_token}"


def test_get_tn_status_maps_not_found_to_bad_request(fake_http, tnlcm_token):
    # Un 404 de TNLCM se traduce a error de cliente: la TN no existe, no es un
    # fallo del orquestador y no debe reintentarse.
    fake_http.respond(404, text="not found")

    with pytest.raises(tnlcm.TnStatusBadRequestError, match="mapped to 400"):
        tnlcm.get_tn_status("tn-demo")

    assert fake_http.paths == [STATUS_PATH]
