"""G4: recoleccion del dataset -- que devolveria ELCM si `Run.CompressFiles` funcionara.

El unico caso con entrada FABRICADA, y hace falta que lo sea. `Run.CompressFiles`
lanza `AttributeError` en las nueve versiones publicadas de ELCM y su excepcion
aborta el experimento entero, asi que `GET /execution/<id>/results` llega vacio y
la ruta de entrega no se puede ejercitar contra la TN real (ver
docs/INCIDENCIA_ELCM_VERSION_DESPLEGADA.md). No es un fallo evitable desde aqui:
`Run.InfluxToCsv` escribe el CSV pero NO lo registra en `GeneratedFiles`, y
`Run.CompressFiles` es la unica tarea que hace ese registro; sin ella el fichero
nunca entra en el ZIP de resultados.

La entrada, por tanto, se reconstruye. No se inventa: la forma del ZIP y el
contenido de los ficheros se copian de dos ejecuciones reales de la TN
`tn_deveop_22_2` del 23 ago. 2026, leyendo sus logs:

  - ELCM execution 11, `TC_4_Dataset_Csv`   -> el CSV exportado de InfluxDB.
  - ELCM execution 13, `TC_6_Latencia_SLA`  -> medidas de rtt e inventario.

Son dos bundles distintos a proposito. El segundo no contiene ningun CSV, y esa
es su razon de ser: sobre un bundle que solo tuviera CSV, las entregas `csv` y
`files` darian el mismo resultado y congelarlas no probaria nada.

Lo que se congela es la SALIDA de la limpieza, que si es codigo nuestro:
descomprimir, borrar los `.log`, desanidar el ZIP interno y devolver la lista.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.adapters import elcm
from app.storage.results_bundle import extract_csv_bundle, extract_results_bundle

pytestmark = pytest.mark.usefixtures("isolate_artifacts_dir")

EXECUTION_LOG = "2026-08-23 17:38:43,276 - INFO - Finished (status: Finished, verdict: Pass)\n"


# --- Ejecucion 11: `TC_4_Dataset_Csv` --------------------------------------

DATASET_ELCM_ID = "11"

# CSV tal como salio de `Run.InfluxToCsv`, reconstruido desde el log, que
# registra las filas exportadas una a una.
#
# Dos rasgos que solo se ven ejecutandolo de verdad, y por eso vale la pena
# congelarlos:
#   - El round-trip por InfluxDB REORDENA las columnas. La entrada era
#     `Timestamp,sample,load1,mem_avail_mb,disk_used_pct`; a la salida van primero
#     el tiempo y los tags, y despues los campos EN ORDEN ALFABETICO.
#   - Los tags son los que ELCM declara en su log al convertir:
#     ['ExecutionId', 'appname', 'facility', 'host', 'hostname']. `facility` sale
#     vacio y no llega a aparecer como columna.
#
# Los timestamps NO estan equiespaciados pese a `DatasetInterval: 1`: falta el
# segundo :35. El bucle del TestCase hace su trabajo Y ADEMAS duerme 1 s, asi que
# el periodo real supera al nominal y 10 muestras ocupan 11 s. Es la razon por la
# que de este TestCase solo se puede congelar la FORMA, nunca los datos.
DATASET_CSV = """\
_time,ExecutionId,_measurement,appname,host,hostname,disk_used_pct,load1,mem_avail_mb,sample
2026-08-23T17:38:32Z,11,DAAS_SELFTEST,ELCM,127.0.0.1,tn-deveop-22-2-elcm-exp,37,0.02,1509,1
2026-08-23T17:38:33Z,11,DAAS_SELFTEST,ELCM,127.0.0.1,tn-deveop-22-2-elcm-exp,37,0.02,1509,2
2026-08-23T17:38:34Z,11,DAAS_SELFTEST,ELCM,127.0.0.1,tn-deveop-22-2-elcm-exp,37,0.02,1509,3
2026-08-23T17:38:36Z,11,DAAS_SELFTEST,ELCM,127.0.0.1,tn-deveop-22-2-elcm-exp,37,0.02,1509,4
2026-08-23T17:38:37Z,11,DAAS_SELFTEST,ELCM,127.0.0.1,tn-deveop-22-2-elcm-exp,37,0.02,1509,5
2026-08-23T17:38:38Z,11,DAAS_SELFTEST,ELCM,127.0.0.1,tn-deveop-22-2-elcm-exp,37,0.02,1509,6
2026-08-23T17:38:39Z,11,DAAS_SELFTEST,ELCM,127.0.0.1,tn-deveop-22-2-elcm-exp,37,0.02,1509,7
2026-08-23T17:38:40Z,11,DAAS_SELFTEST,ELCM,127.0.0.1,tn-deveop-22-2-elcm-exp,37,0.02,1509,8
2026-08-23T17:38:41Z,11,DAAS_SELFTEST,ELCM,127.0.0.1,tn-deveop-22-2-elcm-exp,37,0.02,1509,9
2026-08-23T17:38:42Z,11,DAAS_SELFTEST,ELCM,127.0.0.1,tn-deveop-22-2-elcm-exp,37,0.02,1509,10
"""


# --- Ejecucion 13: `TC_6_Latencia_SLA` -------------------------------------

LATENCY_ELCM_ID = "13"

# Las tres medidas que `Flow.Repeat` fue acumulando, una por vuelta. El log las
# muestra ya colapsadas por `paste -sd,` en "1.000,0.868,0.805".
RTT_SAMPLES = "1.000\n0.868\n0.805\n"

# Inventario del entorno (Order 650), recortado a las secciones que identifican
# la maquina; el log trae ademas disco, memoria y cpu.
ENVIRONMENT_INVENTORY = """\
=== fecha ===
2026-08-23T17:43:32+00:00
=== host ===
tn-deveop-22-2-elcm-exp
=== interfaces ===
lo               UNKNOWN        127.0.0.1/8 ::1/128
eth0             UP             192.168.199.3/24 fe80::c0ff:fea8:c703/64
=== rutas ===
default via 192.168.199.1 dev eth0 onlink
192.168.199.0/24 dev eth0 proto kernel scope link src 192.168.199.3
"""


def _elcm_results_zip(inner_name: str, members: dict[str, str], log_id: str) -> bytes:
    """Arma el `<id>.zip` que sirve ELCM en `/execution/<id>/results`.

    Reproduce la forma real, que tiene dos rarezas heredadas del backend:
      - El ZIP externo es PLANO (`Compress.Zip(..., flat=True)`): los logs y el
        ZIP interno cuelgan de la raiz, sin directorios.
      - Los miembros del ZIP interno llevan BARRA INICIAL, que `zipfile`
        normaliza al extraer. Se reproduce a proposito: si algun dia se sustituye
        `extractall` por una extraccion a mano, esa barra es lo que decide si el
        fichero acaba dentro o fuera del destino.
    """
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(f"/{name}", content)

    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{log_id}.log", EXECUTION_LOG)
        zf.writestr(inner_name, inner.getvalue())
    return outer.getvalue()


def _render_tree(files: list[Path], root: Path) -> str:
    """Vuelca el resultado como texto: rutas relativas ordenadas y su contenido.

    Un listado de `Path` absolutos no sirve de golden porque lleva el temporal de
    la ejecucion. Relativizar deja lo unico que es nuestro: que ficheros quedan,
    con que nombre y con que contenido.
    """
    lines = [f"# {len(files)} ficheros"]
    for path in sorted(files):
        lines.append(f"--- {path.relative_to(root).as_posix()}")
        lines.append(path.read_text(encoding="utf-8").rstrip("\n"))
    return "\n".join(lines) + "\n"


@pytest.mark.asyncio
async def test_csv_delivery_bundle_is_cleaned_unchanged(golden, fake_http, tmp_path) -> None:
    """Entrega `csv`: del ZIP de ELCM al CSV listo en `result/<experimento>/`.

    Se descarga con el adaptador real sobre un transporte simulado, de modo que
    httpx corre de verdad (URL, cabeceras, `raise_for_status`) y lo unico fingido
    es la red.

    Lo que congela el esperado:
      - Los `.log` desaparecen: son ruido de ELCM, no dataset.
      - El ZIP interno se desanida y se borra; queda el CSV suelto, que es lo que
        el usuario espera encontrar.
      - La barra inicial del miembro interno no saca ningun fichero del destino.
    """
    fake_http.respond(
        200,
        content=_elcm_results_zip(
            f"dataset_{DATASET_ELCM_ID}.zip",
            {f"csv_query_{DATASET_ELCM_ID}.csv": DATASET_CSV},
            DATASET_ELCM_ID,
        ),
    )
    dest_dir = tmp_path / "result" / "exp-dataset-completo"
    zip_path = dest_dir / f"files_results_{DATASET_ELCM_ID}.zip"

    await elcm.download_execution_results(
        DATASET_ELCM_ID, str(zip_path), elcm_base_url="http://elcm.test:8080"
    )
    csv_files = extract_csv_bundle(zip_path, dest_dir)

    assert fake_http.paths == [f"/elcm/api/v1/execution/{DATASET_ELCM_ID}/results"]
    golden(_render_tree(csv_files, dest_dir), "bundle_csv/result_tree.txt")


@pytest.mark.asyncio
async def test_files_delivery_keeps_everything_but_the_logs(golden, fake_http, tmp_path) -> None:
    """Entrega `files`: conserva TODO lo que produjo el experimento, salvo los logs.

    Usa el bundle de `TC_6_Latencia_SLA`, que no genera ningun CSV: solo las
    medidas de rtt y el inventario del entorno. La diferencia entre `files` y
    `csv` es una sola linea (`extract_csv_bundle` filtra a `.csv`), asi que si
    ese filtro se colara en la ruta de `files` el esperado pasaria de dos
    ficheros a cero.
    """
    fake_http.respond(
        200,
        content=_elcm_results_zip(
            f"latencia_{LATENCY_ELCM_ID}.zip",
            {
                f"rtt_{LATENCY_ELCM_ID}.txt": RTT_SAMPLES,
                f"entorno_{LATENCY_ELCM_ID}.txt": ENVIRONMENT_INVENTORY,
            },
            LATENCY_ELCM_ID,
        ),
    )
    dest_dir = tmp_path / "result" / "exp-latencia"
    zip_path = dest_dir / f"files_results_{LATENCY_ELCM_ID}.zip"

    await elcm.download_execution_results(
        LATENCY_ELCM_ID, str(zip_path), elcm_base_url="http://elcm.test:8080"
    )
    all_files = extract_results_bundle(zip_path, dest_dir)

    golden(_render_tree(all_files, dest_dir), "bundle_files/result_tree.txt")
