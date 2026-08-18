"""Recoleccion del dataset una vez el experimento ha terminado.

Cuatro entregas con dos origenes distintos: `csv` y `files` salen del ZIP que
sirve ELCM; `dashboard` y `raw` se derivan del resumen del report de TNLCM
(URL de Grafana y consulta directa a InfluxDB).

Ninguna aborta la fase si falla: el experimento ya se ejecuto y sus datos estan
en InfluxDB, asi que lo que se pierde es la entrega, no la medida.
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from app.adapters import elcm, influx
from app.adapters.elcm import ElcmResultsNotFoundError
from app.storage import artifacts, results_bundle

logger = logging.getLogger(__name__)

# Puertos estandar en la VM de monitorizacion (8086 Influx, 3000 Grafana,
# 9090 Prometheus). Grafana -> URL del dashboard; Influx -> consulta raw.
GRAFANA_PORT = 3000
INFLUX_PORT = 8086


async def collect_csv(
    execution_id: str,
    elcm_execution_id: str,
    elcm_base_url: str,
    experiment_name: str | None = None,
) -> list[str]:
    """Descargar el ZIP de resultados de ELCM y extraer el/los CSV en result/.

    Devuelve las rutas de los CSV extraídos (para registrarlos como artifacts).
    Si ELCM todavía no tiene resultados (404) se registra un aviso y se devuelve
    lista vacía, sin abortar la fase.
    """
    result_dir = Path(artifacts._artifact_result_dir(execution_id, experiment_name))
    result_dir.mkdir(parents=True, exist_ok=True)
    zip_path = result_dir / f"csv_results_{elcm_execution_id}.zip"

    try:
        await elcm.download_execution_results(
            elcm_execution_id,
            dest_path=str(zip_path),
            elcm_base_url=elcm_base_url,
            execution_id=execution_id,
        )
    except ElcmResultsNotFoundError as exc:
        logger.warning("[%s] No CSV results available: %s", execution_id, exc)
        return []

    # Extracción/limpieza es I/O de disco -> fuera del event loop (§8.1).
    csv_files = await asyncio.to_thread(results_bundle.extract_csv_bundle, zip_path, result_dir)

    # El ZIP externo ya no hace falta una vez extraído el CSV.
    try:
        zip_path.unlink()
    except OSError:
        pass

    logger.info("[%s] CSV dataset collected: %d file(s)", execution_id, len(csv_files))
    return [str(path) for path in csv_files]


async def collect_files(
    execution_id: str,
    elcm_execution_id: str,
    elcm_base_url: str,
    experiment_name: str | None = None,
) -> list[str]:
    """Descargar el ZIP de resultados de ELCM y extraer TODOS los ficheros en result/.

    Igual que la entrega csv pero SIN inyectar TestCase: recoge los archivos que el
    experimento haya producido, borra los .log y descomprime los ZIP internos,
    quedándose con todos los ficheros. 404 -> aviso + lista vacía (no aborta).
    """
    result_dir = Path(artifacts._artifact_result_dir(execution_id, experiment_name))
    result_dir.mkdir(parents=True, exist_ok=True)
    zip_path = result_dir / f"files_results_{elcm_execution_id}.zip"

    try:
        await elcm.download_execution_results(
            elcm_execution_id,
            dest_path=str(zip_path),
            elcm_base_url=elcm_base_url,
            execution_id=execution_id,
        )
    except ElcmResultsNotFoundError as exc:
        logger.warning("[%s] No files results available: %s", execution_id, exc)
        return []

    # Extracción/limpieza es I/O de disco -> fuera del event loop (§8.1).
    files = await asyncio.to_thread(results_bundle.extract_results_bundle, zip_path, result_dir)

    # El ZIP externo ya no hace falta una vez extraído.
    try:
        zip_path.unlink()
    except OSError:
        pass

    logger.info("[%s] Files dataset collected: %d file(s)", execution_id, len(files))
    return [str(path) for path in files]


async def collect_dashboard(
    execution_id: str, elcm_execution_id: str, experiment_name: str | None = None
) -> list[str]:
    """Construir la URL del dashboard Grafana y guardarla en result/.

    URL: http://<IP_monitoring>:<GRAFANA_PORT>/d/Run<elcm_execution_id>. ELCM crea
    el dashboard con uid Run<id> al ejecutar el TestCase de grafana; la IP de
    monitorización se toma del report TNLCM persistido. No se verifica que el
    dashboard exista: solo se entrega la URL.
    """
    monitoring = artifacts.load_monitoring_info(execution_id)
    ip = monitoring.get("ip")
    if not ip:
        raise ValueError(
            f"Cannot build dashboard URL: monitoring IP missing in TNLCM report "
            f"for execution {execution_id}"
        )

    uid = f"Run{elcm_execution_id}"
    url = f"http://{ip}:{GRAFANA_PORT}/d/{uid}"

    result_dir = Path(artifacts._artifact_result_dir(execution_id, experiment_name))
    result_dir.mkdir(parents=True, exist_ok=True)
    dashboard_path = result_dir / "dashboard.json"
    dashboard_path.write_text(
        json.dumps(
            {
                "output": "dashboard",
                "url": url,
                "grafana_uid": uid,
                "elcm_execution_id": elcm_execution_id,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("[%s] Dashboard URL collected: %s", execution_id, url)
    return [str(dashboard_path)]


async def collect_raw(
    execution_id: str,
    elcm_execution_id: str,
    experiment_name: str | None = None,
    dataset_variables: dict[str, Any] | None = None,
) -> list[str]:
    """Consultar InfluxDB directamente (Flux, 2 pasos) y volcar el CSV crudo en result/.

    Réplica de la interfaz east/west de ELCM: descubre los measurements de la
    ejecución y vuelca cada uno a `raw_<measurement>.csv`. IP/token/org/bucket
    salen del bloque monitoring del report TNLCM (el token en memoria, §8.7).
    Fail-fast si falta la IP o el token de Influx.

    Las variables `dataset.influx_bucket` y `dataset.measurement` del body, si se
    indican, mandan sobre el bucket del report y acotan el volcado a un único
    measurement.
    """
    variables = dataset_variables or {}
    monitoring = artifacts.load_monitoring_info(execution_id)
    ip = monitoring.get("ip")
    credentials = monitoring.get("credentials") or {}
    token = credentials.get("token")
    org = credentials.get("organization") or "testing"
    bucket = variables.get("influx_bucket") or credentials.get("bucket") or "testing"

    if not ip or not token:
        raise ValueError(
            f"Cannot query raw InfluxDB data: missing monitoring ip/token in TNLCM "
            f"report for execution {execution_id}"
        )

    measurements = await influx.collect_raw_measurements(
        host=ip,
        port=INFLUX_PORT,
        org=org,
        bucket=bucket,
        token=token,
        execution_id=elcm_execution_id,
    )

    # Si el body acotó el dataset a un measurement concreto, se respeta. Pedir uno
    # que no existe es un error de configuración, no un resultado vacío.
    wanted = variables.get("measurement")
    if wanted:
        if wanted not in measurements:
            raise ValueError(
                f"dataset.measurement '{wanted}' not found in InfluxDB for ELCM execution "
                f"{elcm_execution_id}. Available: {', '.join(sorted(measurements)) or 'none'}"
            )
        measurements = {wanted: measurements[wanted]}

    result_dir = Path(artifacts._artifact_result_dir(execution_id, experiment_name))
    result_dir.mkdir(parents=True, exist_ok=True)

    raw_paths: list[str] = []
    for measurement, csv_text in measurements.items():
        # Sanear el nombre del measurement para usarlo como nombre de fichero.
        safe = re.sub(r"\W+", "_", measurement) or "measurement"
        raw_path = result_dir / f"raw_{safe}.csv"
        await asyncio.to_thread(raw_path.write_text, csv_text, "utf-8")
        raw_paths.append(str(raw_path))

    logger.info("[%s] Raw dataset collected: %d measurement(s)", execution_id, len(raw_paths))
    return raw_paths
