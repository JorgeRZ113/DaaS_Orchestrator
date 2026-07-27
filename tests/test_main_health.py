import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)


def test_protected_endpoint_rejects_invalid_api_key() -> None:
    response = client.post("/refresh", headers={"x-api-key": "bad-key"})

    assert response.status_code == 401


def test_refresh_endpoint_returns_timeout_hint_when_vpn_is_down(monkeypatch) -> None:
    def _raise_timeout() -> str:
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("app.tnlcm.login_tnlcm_and_persist_token", _raise_timeout)

    response = client.post(
        "/login",
        headers={"x-api-key": settings.api_key},
    )

    assert response.status_code == 504
    assert "VPN" in response.json()["detail"]


def test_refresh_endpoint_returns_token_preview(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.tnlcm.login_tnlcm_and_persist_token",
        lambda: "abcdefghijklmnopqrstuvwxyz123456",
    )

    response = client.post(
        "/login",
        headers={"x-api-key": settings.api_key},
    )

    assert response.status_code == 200
    assert response.json()["message"] == "TNLCM token refreshed and stored in memory"
    assert response.json()["token_preview"].startswith("abcdefghijkl")
