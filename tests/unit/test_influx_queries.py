"""Construccion de consultas Flux y parseo del CSV anotado de InfluxDB.

Logica pura: ni red ni disco. Vivia en `adapters/` porque prueba un modulo
adaptador, pero el nivel lo marcan los recursos que se usan, no la capa de la
aplicacion a la que pertenece el codigo.
"""

import pytest

from app.adapters import influx


def test_build_queries_contain_expected_fields():
    q1 = influx.build_distinct_measurements_query("testing", "9")
    assert 'bucket: "testing"' in q1
    assert 'r["ExecutionId"] == "9"' in q1
    assert "distinct" in q1

    q2 = influx.build_measurement_data_query("testing", "OPEN5GS_KPIS", "9")
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
    assert influx.parse_distinct_measurements(csv_text) == ["OPEN5GS_KPIS", "node_exporter"]


def test_parse_distinct_measurements_empty_returns_empty():
    assert influx.parse_distinct_measurements("") == []


@pytest.mark.asyncio
async def test_collect_raw_measurements_runs_two_step(monkeypatch):
    """Descubrir los measurements y volcarlos son dos consultas, no una."""
    calls: list[str] = []

    async def fake_query(*, host, port, org, token, flux, timeout=60):
        calls.append(flux)
        if "distinct" in flux:
            return "#datatype,string,long,string\r\n,result,table,_value\r\n,,0,OPEN5GS_KPIS\r\n"
        return "time,_value\r\n1,2\r\n"

    monkeypatch.setattr(influx, "query_flux_csv", fake_query)

    results = await influx.collect_raw_measurements(
        host="h", port=8086, org="testing", bucket="testing", token="t", execution_id="9"
    )

    assert list(results.keys()) == ["OPEN5GS_KPIS"]
    assert results["OPEN5GS_KPIS"].startswith("time,_value")
    assert len(calls) == 2  # 1 distinct + 1 measurement
