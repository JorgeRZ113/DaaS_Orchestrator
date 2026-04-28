import pytest
import httpx

from app import tnlcm


def _response(status_code: int, body: str = "") -> httpx.Response:
    request = httpx.Request("PUT", "http://tnlcm.local/api/v1/trial-networks/demo/activate")
    return httpx.Response(status_code=status_code, text=body, request=request)


@pytest.mark.asyncio
async def test_activate_with_backoff_retries_then_succeeds(monkeypatch):
    sleeps: list[int] = []
    attempts = {"count": 0}

    async def _fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    async def _request_call() -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return _response(500, "backend temporary error")
        return _response(200, "ok")

    monkeypatch.setattr(tnlcm.asyncio, "sleep", _fake_sleep)

    await tnlcm._activate_with_backoff(_request_call, "tn-demo", "new")

    assert attempts["count"] == 2
    assert sleeps == [tnlcm.TNLCM_ACTIVATE_RETRY_BASE_DELAY]


@pytest.mark.asyncio
async def test_activate_with_backoff_exhausts_retryable_failures(monkeypatch):
    sleeps: list[int] = []
    attempts = {"count": 0}

    async def _fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    async def _request_call() -> httpx.Response:
        attempts["count"] += 1
        return _response(503, "service unavailable")

    monkeypatch.setattr(tnlcm.asyncio, "sleep", _fake_sleep)

    with pytest.raises(tnlcm._ActivateRetryExhaustedError):
        await tnlcm._activate_with_backoff(_request_call, "tn-demo", "legacy")

    assert attempts["count"] == tnlcm.TNLCM_ACTIVATE_MAX_ATTEMPTS
    assert sleeps == [tnlcm.TNLCM_ACTIVATE_RETRY_BASE_DELAY]


@pytest.mark.asyncio
async def test_recover_tn_with_destroy_purge_uses_delays_and_cleanup(monkeypatch):
    calls: list[str] = []

    async def _fake_sleep(seconds: int) -> None:
        calls.append(f"sleep:{seconds}")

    async def _fake_destroy(tn_id: str) -> None:
        calls.append(f"destroy:{tn_id}")

    monkeypatch.setattr(tnlcm, "TNLCM_RECOVERY_DESTROY_DELAY", 1)
    monkeypatch.setattr(tnlcm, "TNLCM_REDEPLOY_DELAY", 2)
    monkeypatch.setattr(tnlcm.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(tnlcm, "destroy_trial_network", _fake_destroy)

    await tnlcm._recover_tn_with_destroy_purge("tn-demo")

    assert calls == ["sleep:1", "destroy:tn-demo", "sleep:2"]
