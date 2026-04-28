import httpx
import pytest

from app import tnlcm
from app.models import InfrastructureConfig


class _FakeAsyncClient:
    def __init__(self, legacy_response: httpx.Response, activate_response: httpx.Response | None = None):
        self._legacy_response = legacy_response
        self._activate_response = activate_response or _response(
            method="PUT",
            url="http://tnlcm.local/api/v1/trial-networks/tn-demo/activate",
            status_code=200,
            payload={"status": "ok"},
        )
        self.post_calls: list[str] = []
        self.put_calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, **kwargs) -> httpx.Response:
        self.post_calls.append(url)
        if url.endswith("/api/v1/trial-network/legacy"):
            return self._legacy_response
        pytest.fail(f"Unexpected POST endpoint called: {url}")

    async def put(self, url: str, **kwargs) -> httpx.Response:
        self.put_calls.append(url)
        return self._activate_response


def _response(method: str, url: str, status_code: int, payload: dict[str, str]) -> httpx.Response:
    request = httpx.Request(method, url)
    return httpx.Response(status_code=status_code, json=payload, request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "backend_message"),
    [
        (400, "estado no permitido"),
        (404, "recursos no encontrados"),
        (422, "descriptor invalido"),
    ],
)
async def test_deploy_trial_network_legacy_client_errors_are_not_retried(
    monkeypatch,
    status_code,
    backend_message,
):
    fake_client = _FakeAsyncClient(
        legacy_response=_response(
            method="POST",
            url="http://tnlcm.local/api/v1/trial-network/legacy",
            status_code=status_code,
            payload={"message": backend_message},
        )
    )

    monkeypatch.setattr(tnlcm.httpx, "AsyncClient", lambda timeout=None: fake_client)
    monkeypatch.setattr(tnlcm, "_legacy_multipart_from_infra", lambda infra: ({}, {}))
    monkeypatch.setattr(tnlcm, "_headers", lambda: {})

    infra = InfrastructureConfig(name="tn-demo", descriptor_path="desc.yaml", parameters={})

    with pytest.raises(RuntimeError) as exc_info:
        await tnlcm.deploy_trial_network(infra)

    message = str(exc_info.value)
    assert backend_message in message
    assert tnlcm.TNLCM_LEGACY_ERROR_HINT in message
    assert len(fake_client.post_calls) == 1
    assert fake_client.post_calls[0].endswith("/api/v1/trial-network/legacy")
    assert fake_client.put_calls == []


@pytest.mark.asyncio
async def test_deploy_trial_network_legacy_201_ok_continues_flow(monkeypatch):
    sleep_calls: list[int] = []

    async def _fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)

    fake_client = _FakeAsyncClient(
        legacy_response=_response(
            method="POST",
            url="http://tnlcm.local/api/v1/trial-network/legacy",
            status_code=201,
            payload={"tn_id": "tn-demo"},
        )
    )

    monkeypatch.setattr(tnlcm.httpx, "AsyncClient", lambda timeout=None: fake_client)
    monkeypatch.setattr(tnlcm, "_legacy_multipart_from_infra", lambda infra: ({}, {}))
    monkeypatch.setattr(tnlcm, "_headers", lambda: {})
    monkeypatch.setattr(tnlcm.asyncio, "sleep", _fake_sleep)

    infra = InfrastructureConfig(name="tn-demo", descriptor_path="desc.yaml", parameters={})
    tn_id = await tnlcm.deploy_trial_network(infra)

    assert tn_id == "tn-demo"
    assert sleep_calls == [20]
    assert len(fake_client.post_calls) == 1
    assert fake_client.post_calls[0].endswith("/api/v1/trial-network/legacy")
    assert len(fake_client.put_calls) == 1
    assert fake_client.put_calls[0].endswith("/api/v1/trial-networks/tn-demo/activate")


