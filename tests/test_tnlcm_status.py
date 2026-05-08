import httpx
import pytest

from app import tnlcm


@pytest.fixture(autouse=True)
def _load_in_memory_token():
    previous_access = tnlcm._tnlcm_access_token
    previous_refresh = tnlcm._tnlcm_refresh_token
    tnlcm._tnlcm_access_token = "test-token"
    tnlcm._tnlcm_refresh_token = None
    yield
    tnlcm._tnlcm_access_token = previous_access
    tnlcm._tnlcm_refresh_token = previous_refresh


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


def test_extract_elcm_url_from_report_uses_fixed_backend_port_5001() -> None:
    report_summary = {
        "components": {
            "tn-demo2_4-elcm-exp": {
                "ips": ["192.168.199.3"],
                "ports": [5000, 5001],
            }
        }
    }

    assert tnlcm.extract_elcm_url_from_report(report_summary) == "http://192.168.199.3:5001"


def test_extract_elcm_url_from_report_returns_none_when_component_missing() -> None:
    report_summary = {
        "components": {
            "tn-demo2_4-monitoring-test": {
                "ips": ["192.168.199.2"],
                "ports": [3000, 8086],
            }
        }
    }

    assert tnlcm.extract_elcm_url_from_report(report_summary) is None
