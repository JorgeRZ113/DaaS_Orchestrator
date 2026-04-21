from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health_endpoint_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_protected_endpoint_rejects_invalid_api_key() -> None:
    response = client.post("/config/reload", headers={"x-api-key": "bad-key"})

    assert response.status_code == 401
