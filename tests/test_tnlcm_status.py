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


def _response(status_code: int, body: str = "{}") -> httpx.Response:
    request = httpx.Request("GET", "http://tnlcm.local/api/v1/trial-networks/tn-demo")
    return httpx.Response(status_code=status_code, text=body, request=request)


def test_get_tn_status_is_sync_and_returns_status(monkeypatch):
    monkeypatch.setattr(
        tnlcm.httpx,
        "Client",
        lambda timeout: _FakeClient(_response(200, '{"status": "ACTIVE"}')),
    )

    assert tnlcm.get_tn_status("tn-demo") == "ACTIVE"


def test_get_tn_status_maps_not_found_to_bad_request(monkeypatch):
    monkeypatch.setattr(
        tnlcm.httpx, "Client", lambda timeout: _FakeClient(_response(404, "not found"))
    )

    with pytest.raises(tnlcm.TnStatusBadRequestError, match="mapped to 400"):
        tnlcm.get_tn_status("tn-demo")
