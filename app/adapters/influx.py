"""
Consulta cruda ("raw") de InfluxDB v2 vía Flux.

Réplica de lo que hace la interfaz east/west de ELCM (elcm/Helper/influx.py:
`export_influxdb_v2` + `GetExecutionMeasurements`): en vez de pasar por un
TestCase, se consulta InfluxDB directamente por HTTP.

Flujo de 2 pasos:
  1. Descubrir los `_measurement` distintos de una ejecución (por `ExecutionId`).
  2. Volcar, por cada measurement, todos sus puntos filtrados por `ExecutionId`.

La salida es el CSV anotado que devuelve InfluxDB (`Accept: application/csv`),
guardado tal cual (crudo).

Notas:
- Red con httpx async + timeout explícito (§8.1).
- El `token` es secreto: se recibe por parámetro (desde el report en memoria),
  NUNCA se loguea ni se persiste (§8.7).
"""

from __future__ import annotations

import csv as csv_module
import io
import logging

import httpx

logger = logging.getLogger(__name__)

INFLUX_QUERY_TIMEOUT = 60


def build_distinct_measurements_query(bucket: str, execution_id: str) -> str:
    """Flux del paso 1: measurements distintos de la ejecución (como GetExecutionMeasurements)."""
    return (
        f'from(bucket: "{bucket}")\n'
        "  |> range(start: 0)\n"
        f'  |> filter(fn: (r) => r["ExecutionId"] == "{execution_id}")\n'
        '  |> keep(columns: ["_measurement"])\n'
        '  |> distinct(column: "_measurement")'
    )


def build_measurement_data_query(bucket: str, measurement: str, execution_id: str) -> str:
    """Flux del paso 2: todos los puntos de un measurement para la ejecución."""
    return (
        f'from(bucket: "{bucket}")\n'
        "  |> range(start: 0)\n"
        f'  |> filter(fn: (r) => r["_measurement"] == "{measurement}")\n'
        f'  |> filter(fn: (r) => r["ExecutionId"] == "{execution_id}")'
    )


async def query_flux_csv(
    *,
    host: str,
    port: int,
    org: str,
    token: str,
    flux: str,
    timeout: int = INFLUX_QUERY_TIMEOUT,
) -> str:
    """POST una query Flux a InfluxDB v2 y devolver la respuesta CSV.

    Réplica de `export_influxdb_v2` de ELCM. No loguea el token ni las cabeceras.

    Raises:
        RuntimeError: si InfluxDB responde con un código de error.
    """
    url = f"http://{host}:{port}/api/v2/query?org={org}"
    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/csv",
        "Content-Type": "application/vnd.flux",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(url, headers=headers, content=flux)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"InfluxDB query failed (HTTP {exc.response.status_code})") from exc
        return response.text


def parse_distinct_measurements(csv_text: str) -> list[str]:
    """Extraer los nombres de measurement del CSV anotado del paso 1.

    InfluxDB devuelve el valor de `distinct()` en la columna `_value`. Se ignoran
    las filas de anotación (`#datatype`/`#group`/`#default`) y las vacías.
    """
    measurements: list[str] = []
    header: list[str] | None = None
    value_idx: int | None = None

    for row in csv_module.reader(io.StringIO(csv_text)):
        if not row or all(not cell.strip() for cell in row):
            header = None  # separación entre bloques
            continue
        if row[0].startswith("#"):
            header = None  # anotaciones -> empieza un bloque nuevo
            continue
        if header is None:
            header = row
            value_idx = row.index("_value") if "_value" in row else None
            continue
        if value_idx is not None and value_idx < len(row):
            value = row[value_idx].strip()
            if value:
                measurements.append(value)

    # Dedup conservando el orden de aparición.
    seen: set[str] = set()
    unique: list[str] = []
    for measurement in measurements:
        if measurement not in seen:
            seen.add(measurement)
            unique.append(measurement)
    return unique


async def collect_raw_measurements(
    *,
    host: str,
    port: int,
    org: str,
    bucket: str,
    token: str,
    execution_id: str,
) -> dict[str, str]:
    """Ejecutar el flujo de 2 pasos y devolver {measurement: csv_crudo}."""
    distinct_csv = await query_flux_csv(
        host=host,
        port=port,
        org=org,
        token=token,
        flux=build_distinct_measurements_query(bucket, execution_id),
    )
    measurements = parse_distinct_measurements(distinct_csv)
    logger.info("Raw influx: %d measurement(s) for ExecutionId=%s", len(measurements), execution_id)

    results: dict[str, str] = {}
    for measurement in measurements:
        results[measurement] = await query_flux_csv(
            host=host,
            port=port,
            org=org,
            token=token,
            flux=build_measurement_data_query(bucket, measurement, execution_id),
        )
    return results
