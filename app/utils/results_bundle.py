"""
Extracción y limpieza del bundle de resultados de ELCM (entrega csv).

El ZIP que sirve ELCM en `GET /elcm/api/v1/execution/<id>/results` contiene, de
forma plana (ver `Compress.Zip(..., flat=True)` en el backend), los ficheros
generados por la ejecución: los logs (`.log`) y —cuando corre el TestCase de
dataset csv— un ZIP interno (p. ej. `dataset_<id>.zip`) con el/los CSV dentro.

La entrega `csv` consiste en quedarnos solo con el/los CSV:
  1. Descomprimir el ZIP externo en `dest_dir`.
  2. Borrar los `.log`.
  3. Descomprimir cada ZIP interno restante y borrarlo.

Todo es I/O de disco síncrono: al llamarlo desde código async, envolver en
`await asyncio.to_thread(extract_csv_bundle, ...)` (regla §8.1 de no bloquear
el event loop).
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _safe_extract(zip_file: Path, dest_dir: Path) -> None:
    """Extraer `zip_file` en `dest_dir`.

    zipfile (Python 3.6.2+) sanea los nombres de miembro al extraer: descarta la
    unidad y los separadores iniciales y elimina los componentes '..', de modo
    que ningún fichero puede escribirse fuera de `dest_dir` (protección frente a
    Zip Slip). Los ZIP internos de ELCM traen el CSV con nombre '/csv_...csv'
    (barra inicial), que zipfile normaliza a un fichero dentro de `dest_dir`.
    """
    with zipfile.ZipFile(zip_file) as zf:
        zf.extractall(dest_dir)


def extract_csv_bundle(zip_path: str | Path, dest_dir: str | Path) -> list[Path]:
    """
    Extraer el ZIP de resultados y dejar en `dest_dir` solo el/los CSV.

    Args:
        zip_path: Ruta al ZIP externo descargado de ELCM.
        dest_dir: Directorio destino (normalmente artifacts/<id>/result/).

    Returns:
        Lista de rutas a los `.csv` resultantes (ordenada).

    Raises:
        ValueError: si `zip_path` no es un ZIP válido o contiene rutas inseguras.
    """
    zip_path = Path(zip_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"Not a valid zip file: {zip_path}")

    # 1) Descomprimir el ZIP externo.
    _safe_extract(zip_path, dest_dir)

    # 2) Borrar los .log extraídos.
    for log_file in dest_dir.rglob("*.log"):
        log_file.unlink()
        logger.debug("Removed log file: %s", log_file)

    # 3) Descomprimir los ZIP internos restantes (el del csv) y borrarlos.
    #    Se excluye el propio ZIP externo por si vive dentro de dest_dir.
    zip_resolved = zip_path.resolve()
    inner_zips = [p for p in dest_dir.rglob("*.zip") if p.resolve() != zip_resolved]
    for inner in inner_zips:
        if zipfile.is_zipfile(inner):
            _safe_extract(inner, dest_dir)
            logger.debug("Extracted inner zip: %s", inner)
        inner.unlink()

    return sorted(dest_dir.rglob("*.csv"))
