import httpx
import pytest

from app import tnlcm
from app.models import InfrastructureConfig


class _FakeAsyncClient:
    def __init__(
        self,
        legacy_response: httpx.Response,
        activate_response: httpx.Response | None = None,
        status_response: httpx.Response | None = None,
    ):
        self._legacy_response = legacy_response
        self._activate_response = activate_response or _response(
            method="PUT",
            url="http://tnlcm.local/api/v1/trial-networks/tn-demo/activate",
            status_code=200,
            payload={"status": "ok"},
        )
        # GET /trial-networks/{tn_id} usado por la reconciliación. Por defecto la
        # TN no existe (404): así un 400 real de create/activate sigue fallando.
        self._status_response = status_response
        self.post_calls: list[str] = []
        self.put_calls: list[str] = []
        self.get_calls: list[str] = []

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

    async def get(self, url: str, **kwargs) -> httpx.Response:
        self.get_calls.append(url)
        if self._status_response is not None:
            return self._status_response
        return _response(
            method="GET",
            url=url,
            status_code=404,
            payload={"message": "trial network not found"},
        )


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


def _patch_deploy_dependencies(monkeypatch, fake_client, sleep_calls: list[int]) -> None:
    async def _fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(tnlcm.httpx, "AsyncClient", lambda timeout=None: fake_client)
    monkeypatch.setattr(tnlcm, "_legacy_multipart_from_infra", lambda infra: ({}, {}))
    monkeypatch.setattr(tnlcm, "_headers", lambda: {})
    monkeypatch.setattr(tnlcm.asyncio, "sleep", _fake_sleep)


@pytest.mark.asyncio
async def test_deploy_reconciles_when_create_400_and_state_activated(monkeypatch):
    """Un create 400 sobre una TN ya 'activated' salta create+activate y no espera."""
    sleep_calls: list[int] = []
    fake_client = _FakeAsyncClient(
        legacy_response=_response(
            method="POST",
            url="http://tnlcm.local/api/v1/trial-network/legacy",
            status_code=400,
            payload={"message": "trial network already exists"},
        ),
        status_response=_response(
            method="GET",
            url="http://tnlcm.local/api/v1/trial-networks/tn-demo",
            status_code=200,
            payload={"tn_id": "tn-demo", "state": "activated"},
        ),
    )
    _patch_deploy_dependencies(monkeypatch, fake_client, sleep_calls)

    infra = InfrastructureConfig(name="tn-demo", descriptor_path="desc.yaml", parameters={})
    tn_id = await tnlcm.deploy_trial_network(infra)

    assert tn_id == "tn-demo"
    assert len(fake_client.post_calls) == 1
    assert len(fake_client.get_calls) == 1
    assert fake_client.put_calls == []  # no se re-activa
    assert sleep_calls == []  # no se espera la ventana de registro de 20s


@pytest.mark.asyncio
async def test_deploy_reconciles_when_create_400_and_state_created(monkeypatch):
    """Un create 400 sobre una TN ya 'created' salta el create pero sí la activa."""
    sleep_calls: list[int] = []
    fake_client = _FakeAsyncClient(
        legacy_response=_response(
            method="POST",
            url="http://tnlcm.local/api/v1/trial-network/legacy",
            status_code=400,
            payload={"message": "trial network already exists"},
        ),
        status_response=_response(
            method="GET",
            url="http://tnlcm.local/api/v1/trial-networks/tn-demo",
            status_code=200,
            payload={"tn_id": "tn-demo", "state": "created"},
        ),
    )
    _patch_deploy_dependencies(monkeypatch, fake_client, sleep_calls)

    infra = InfrastructureConfig(name="tn-demo", descriptor_path="desc.yaml", parameters={})
    tn_id = await tnlcm.deploy_trial_network(infra)

    assert tn_id == "tn-demo"
    assert len(fake_client.post_calls) == 1
    assert len(fake_client.get_calls) == 1
    assert len(fake_client.put_calls) == 1  # se activa
    assert fake_client.put_calls[0].endswith("/api/v1/trial-networks/tn-demo/activate")
    assert sleep_calls == []  # TN ya registrada: sin espera de 20s


@pytest.mark.asyncio
async def test_deploy_activate_400_already_activated_succeeds(monkeypatch):
    """Un 400 al activar se tolera si el estado real confirma que ya está 'activated'."""
    sleep_calls: list[int] = []
    fake_client = _FakeAsyncClient(
        legacy_response=_response(
            method="POST",
            url="http://tnlcm.local/api/v1/trial-network/legacy",
            status_code=201,
            payload={"tn_id": "tn-demo"},
        ),
        activate_response=_response(
            method="PUT",
            url="http://tnlcm.local/api/v1/trial-networks/tn-demo/activate",
            status_code=400,
            payload={"message": "trial network already activated"},
        ),
        status_response=_response(
            method="GET",
            url="http://tnlcm.local/api/v1/trial-networks/tn-demo",
            status_code=200,
            payload={"tn_id": "tn-demo", "state": "activated"},
        ),
    )
    _patch_deploy_dependencies(monkeypatch, fake_client, sleep_calls)

    infra = InfrastructureConfig(name="tn-demo", descriptor_path="desc.yaml", parameters={})
    tn_id = await tnlcm.deploy_trial_network(infra)

    assert tn_id == "tn-demo"
    assert sleep_calls == [20]  # create fresco: sí espera la ventana de registro
    assert len(fake_client.post_calls) == 1
    assert len(fake_client.put_calls) == 1  # se intenta activar (devuelve 400)
    assert len(fake_client.get_calls) == 1  # y se confirma el estado real


@pytest.mark.asyncio
async def test_deploy_create_400_terminal_state_raises_actionable_error(monkeypatch):
    """Un create 400 sobre una TN en estado terminal ('failed') falla con guía clara."""
    sleep_calls: list[int] = []
    fake_client = _FakeAsyncClient(
        legacy_response=_response(
            method="POST",
            url="http://tnlcm.local/api/v1/trial-network/legacy",
            status_code=400,
            payload={"message": "trial network already exists"},
        ),
        status_response=_response(
            method="GET",
            url="http://tnlcm.local/api/v1/trial-networks/tn-demo",
            status_code=200,
            payload={"tn_id": "tn-demo", "state": "failed"},
        ),
    )
    _patch_deploy_dependencies(monkeypatch, fake_client, sleep_calls)

    infra = InfrastructureConfig(name="tn-demo", descriptor_path="desc.yaml", parameters={})

    with pytest.raises(RuntimeError) as exc_info:
        await tnlcm.deploy_trial_network(infra)

    message = str(exc_info.value)
    assert "terminal state" in message
    assert "failed" in message
    assert fake_client.put_calls == []  # no se activa una TN terminal
    assert len(fake_client.get_calls) == 1
