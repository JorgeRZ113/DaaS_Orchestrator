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
        "elcm": {
            "name": "tn-demo2_4-elcm-exp",
            "ips": ["192.168.199.3"],
            "ports": [5000, 5001],
        }
    }

    assert tnlcm.extract_elcm_url_from_report(report_summary) == "http://192.168.199.3:5001"


def test_summarize_trial_network_report_uses_fixed_sections_and_preserves_aux_order() -> None:
    report_markdown = """
# tn-demo-tn_vxlan

The component `tn-demo-tn_vxlan` has been succesfully created.

## Important information:

- **OpenNebula VNet ID**: `514`
- **VXLAN first IP**: `192.168.199.1`

---

# tn-demo-tn_bastion

The component `tn-demo-tn_bastion` has been succesfully created.

## Important information:

- **OpenNebula VM ID**: `2135`
- **VM network interfaces**:
```json
{"514": "192.168.199.1"}
```

#### SSH keypair

**Private key**:
```text
PRIVATE-KEY
```

#### Wireguard VPN client config

**wg_client0**:
```text
WG-CONFIG
```

#### Technitium DNS Server

You can access the Technitium DNS web Portal from [http://bastion.example:5380](http://bastion.example:5380)

Credentials to login are:
- user: `admin`
- password: `secret`

---

# tn-demo-monitoring-test

The component `tn-demo-monitoring-test` has been succesfully created.

## Important information:

- **OpenNebula VM ID**: `2136`
- **VM network interfaces**:
```json
{"514": "192.168.199.2"}
```

## InfluxDB v2.7.11 information:

InfluxDB is available on port 8086.

- **Username**: `admin`
- **Password**: `adminadmin`
- **Organization**: `testing`
- **Bucket**: `testing`
- **Token**: `default-token-testing`

## Grafana v11.6.0

Grafana is available on port 3000.

- **Username**: `admin`
- **Password**: `adminadmin`

## Prometheus v2.54.3

Prometheus is available on port 9090.

---

# tn-demo-elcm-exp

The component `tn-demo-elcm-exp` has been succesfully created.

## Important information:

- **OpenNebula VM ID**: `2137`
- **VM network interfaces**:
```json
{"514": "192.168.199.3"}
```

## ELCM BACKEND

The backend dashboard is available on port 5001.

## ELCM FRONTEND

The frontend dashboard is available on port 5000.

---

# tn-demo-alpha

The component `tn-demo-alpha` has been succesfully created.

## Important information:

- **OpenNebula VM ID**: `3001`
- **VM network interfaces**:
```json
{"777": "10.0.0.1"}
```

---

# tn-demo-beta

The component `tn-demo-beta` has been succesfully created.

## Important information:

- **OpenNebula VM ID**: `3002`
- **VM network interfaces**:
```json
{"778": "10.0.0.2"}
```
"""

    summary = tnlcm.summarize_trial_network_report(report_markdown)

    assert summary["components_count"] == 6
    assert summary["private_ssh_key"] == "PRIVATE-KEY"
    assert summary["wireguard_client_config"] == "WG-CONFIG"
    assert summary["tn_vxlan"] is not None
    assert summary["tn_bastion"] is not None
    assert summary["monitoring"] is not None
    assert summary["elcm"] is not None
    assert summary["tn_vxlan"]["extra_info"]["vxlan_first_ip"] == "192.168.199.1"
    assert summary["technitium_dns"]["username"] == "admin"
    assert summary["monitoring"]["ip"] == "192.168.199.2"
    assert summary["monitoring"]["ports"] == [8086, 3000, 9090]
    assert summary["monitoring"]["credentials"]["token"] == "default-token-testing"
    assert summary["elcm"]["ip"] == "192.168.199.3"
    assert summary["elcm"]["ports"] == [5001, 5000]
    assert "tn-demo-alpha" in summary["components"]
    assert "tn-demo-beta" in summary["components"]


def test_summarize_trial_network_report_sets_missing_fixed_sections_to_null() -> None:
    summary = tnlcm.summarize_trial_network_report("""
# tn-demo-custom

The component `tn-demo-custom` has been succesfully created.

## Important information:

- **OpenNebula VM ID**: `3003`
- **VM network interfaces**:
```json
{"779": "10.0.0.3"}
```
""")

    assert summary["private_ssh_key"] is None
    assert summary["wireguard_client_config"] is None
    assert summary["tn_vxlan"] is None
    assert summary["tn_bastion"] is None
    assert summary["technitium_dns"] is None
    assert summary["monitoring"] is None
    assert summary["elcm"] is None
    assert "tn-demo-custom" in summary["components"]


def test_extract_elcm_url_from_report_returns_none_when_component_missing() -> None:
    report_summary = {
        "monitoring": {
            "name": "tn-demo2_4-monitoring-test",
            "ips": ["192.168.199.2"],
            "ports": [3000, 8086],
        }
    }

    assert tnlcm.extract_elcm_url_from_report(report_summary) is None
