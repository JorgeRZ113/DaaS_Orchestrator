import pytest

from app import tnlcm


@pytest.fixture(autouse=True)
def _reset_tnlcm_tokens():
    previous_access = tnlcm._tnlcm_access_token
    previous_refresh = tnlcm._tnlcm_refresh_token
    tnlcm._tnlcm_access_token = None
    tnlcm._tnlcm_refresh_token = None
    yield
    tnlcm._tnlcm_access_token = previous_access
    tnlcm._tnlcm_refresh_token = previous_refresh


def test_headers_fail_when_token_is_not_loaded_in_memory() -> None:
    with pytest.raises(ValueError, match="Call /tnlcm/token/refresh first"):
        tnlcm._headers()


def test_headers_use_in_memory_token() -> None:
    tnlcm._tnlcm_access_token = "abc123token"

    headers = tnlcm._headers()

    assert headers["Authorization"] == "Bearer abc123token"
    assert headers["Accept"] == "application/json"
