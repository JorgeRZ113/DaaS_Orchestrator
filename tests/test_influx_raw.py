import httpx
import pytest

from app.utils import influx_raw


class _FakePostClient:
    def __init__(self, response: httpx.Response, sink: dict):
        self._response = response
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, content=None, **kwargs):
        self._sink["url"] = url
        self._sink["headers"] = headers
        self._sink["content"] = content
        return self._response


def test_build_queries_contain_expected_fields():
    q1 = influx_raw.build_distinct_measurements_query("testing", "9")
    assert 'bucket: "testing"' in q1
    assert 'r["ExecutionId"] == "9"' in q1
    assert "distinct" in q1

    q2 = influx_raw.build_measurement_data_query("testing", "OPEN5GS_KPIS", "9")
    assert 'r["_measurement"] == "OPEN5GS_KPIS"' in q2
    assert 'r["ExecutionId"] == "9"' in q2


def test_parse_distinct_measurements_parses_annotated_csv():
    csv_text = (
        "#datatype,string,long,string\r\n"
        "#group,false,false,true\r\n"
        "#default,_result,,\r\n"
        ",result,table,_value\r\n"
        ",,0,OPEN5GS_KPIS\r\n"
        ",,0,node_exporter\r\n"
    )
    assert influx_raw.parse_distinct_measurements(csv_text) == ["OPEN5GS_KPIS", "node_exporter"]


def test_parse_distinct_measurements_empty_returns_empty():
    assert influx_raw.parse_distinct_measurements("") == []


@pytest.mark.asyncio
async def test_query_flux_csv_posts_with_token(monkeypatch):
    sink: dict = {}
    request = httpx.Request("POST", "http://192.168.199.2:8086/api/v2/query?org=testing")
    response = httpx.Response(200, text="csv-body", request=request)
    monkeypatch.setattr(
        influx_raw.httpx, "AsyncClient", lambda timeout=None: _FakePostClient(response, sink)
    )

    text = await influx_raw.query_flux_csv(
        host="192.168.199.2", port=8086, org="testing", token="secret-token", flux="from(...)"
    )

    assert text == "csv-body"
    assert sink["url"] == "http://192.168.199.2:8086/api/v2/query?org=testing"
    assert sink["headers"]["Authorization"] == "Token secret-token"
    assert sink["headers"]["Content-Type"] == "application/vnd.flux"
    assert sink["content"] == "from(...)"


@pytest.mark.asyncio
async def test_collect_raw_measurements_runs_two_step(monkeypatch):
    calls: list[str] = []

    async def fake_query(*, host, port, org, token, flux, timeout=60):
        calls.append(flux)
        if "distinct" in flux:
            return "#datatype,string,long,string\r\n,result,table,_value\r\n,,0,OPEN5GS_KPIS\r\n"
        return "time,_value\r\n1,2\r\n"

    monkeypatch.setattr(influx_raw, "query_flux_csv", fake_query)

    results = await influx_raw.collect_raw_measurements(
        host="h", port=8086, org="testing", bucket="testing", token="t", execution_id="9"
    )

    assert list(results.keys()) == ["OPEN5GS_KPIS"]
    assert results["OPEN5GS_KPIS"].startswith("time,_value")
    assert len(calls) == 2  # 1 distinct + 1 measurement
