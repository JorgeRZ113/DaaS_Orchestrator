import httpx
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

client = TestClient(app)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _make_fake_async_client(*, on_get):
    """Crea una clase AsyncClient falsa cuyo `get` delega en `on_get`."""

    class _FakeAsyncClient:
        def __init__(self, timeout=None) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            return on_get(url)

    return _FakeAsyncClient


# --- /health/services -------------------------------------------------------


def test_health_services_reports_ok_when_tnlcm_alive(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.observability.health.httpx.AsyncClient",
        _make_fake_async_client(on_get=lambda url: _FakeResponse(200)),
    )

    response = client.get("/health/services")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["orchestrator"]["alive"] is True
    assert body["tnlcm"]["alive"] is True


def test_health_services_reports_alive_on_http_error_status(monkeypatch) -> None:
    # Un 401/404 significa que TNLCM está escuchando: se considera "vivo".
    monkeypatch.setattr(
        "app.observability.health.httpx.AsyncClient",
        _make_fake_async_client(on_get=lambda url: _FakeResponse(401)),
    )

    response = client.get("/health/services")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["tnlcm"]["alive"] is True


def test_health_services_reports_fallen_when_tnlcm_down(monkeypatch) -> None:
    def _raise_connect_error(url):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        "app.observability.health.httpx.AsyncClient",
        _make_fake_async_client(on_get=_raise_connect_error),
    )

    response = client.get("/health/services")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "fallen"
    assert body["orchestrator"]["alive"] is True
    assert body["tnlcm"]["alive"] is False


# --- /health/components -----------------------------------------------------


def test_health_components_requires_api_key() -> None:
    response = client.get("/health/components")

    assert response.status_code in (401, 422)


def test_health_components_reports_ok_when_all_healthy(monkeypatch) -> None:
    async def _fake_probe(url, healthy_statuses):
        return True

    monkeypatch.setattr("app.observability.health.probe_http_service", _fake_probe)

    response = client.get("/health/components", headers={"x-api-key": settings.api_key})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    by_service = {s["service"]: s for s in body["services"]}
    # Los 4 servicios fijos del diccionario KNOWN_SERVICES.
    assert set(by_service) == {"influxdb", "grafana", "prometheus", "elcm"}
    assert all(s["healthy"] for s in body["services"])
    # La respuesta no expone IPs ni puertos (lo más oculto posible).
    assert "ip" not in by_service["influxdb"]
    assert "port" not in by_service["influxdb"]


def test_health_components_reports_fallen_when_one_down(monkeypatch) -> None:
    from app.observability.health import KNOWN_SERVICES

    prometheus_url = "http://{ip}:{port}{path}".format(**KNOWN_SERVICES["prometheus"])

    async def _fake_probe(url, healthy_statuses):
        if url == prometheus_url:
            return False
        return True

    monkeypatch.setattr("app.observability.health.probe_http_service", _fake_probe)

    response = client.get("/health/components", headers={"x-api-key": settings.api_key})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "fallen"
    by_service = {s["service"]: s for s in body["services"]}
    assert by_service["prometheus"]["healthy"] is False
    assert by_service["influxdb"]["healthy"] is True
