"""
Empaquetado de `artifacts/<execution_id>/` en un ZIP descargable.

Es la contraparte de `results_bundle.py`, que solo EXTRAE los ZIP que sirve
ELCM: aquí se CREA uno con todo lo que ha dejado una ejecución, para que el
experimentador se lleve el resultado completo en un fichero y pueda archivarlo o
adjuntarlo. Cierra la actividad F6.2 del anteproyecto («ZIP final con README de
metadatos»).

Dos cosas que no son negociables y por las que este módulo existe aparte:

**1. Secretos.** La carpeta contiene credenciales en claro (deuda §8.7, abierta):
la config de WireGuard lleva su `PrivateKey` y los informes crudos de TNLCM
llevan el token de InfluxDB y el bloque de credenciales. Por defecto NO viajan;
hay que pedirlos explícitamente. No se excluye en cambio el descriptor ni los
artefactos generados: contienen contraseñas que el propio usuario escribió y sin
ellos el ZIP no sirve para reproducir la ejecución, que es su razón de ser.

**2. Path traversal.** `execution_id` se deriva de `infrastructure.name`, que es
entrada de usuario sin validar (deuda §8.3). En los caminos de escritura eso ya
es feo; en uno de LECTURA como este sería un agujero — `../../etc/passwd` leería
fuera de `artifacts/`. Por eso aquí se valida el identificador y además se
comprueba que la ruta resuelta cae dentro de la raíz, que es la comprobación que
de verdad cierra el caso (los enlaces simbólicos no los para un regex).

Todo es I/O de disco síncrono: al llamarlo desde código async, envolver en
`await asyncio.to_thread(...)` (regla §8.1 de no bloquear el event loop).
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Identificador admisible como componente de ruta. Es el patron que pide
# CLAUDE.md §8.3; se aplica ANTES de tocar el filesystem.
EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Nombre del informe de metadatos que se genera dentro del ZIP.
README_NAME = "README.md"

# Ficheros que llevan CLAVES DE ACCESO (no credenciales que el usuario ya
# conoce) y que por eso quedan fuera salvo peticion explicita.
SECRET_FILENAMES = frozenset(
    {
        # Bloque [Interface] con la PrivateKey del tunel, y la PresharedKey.
        # El nombre real es "<tn_id>.conf", asi que se filtra por extension.
        "tnlcm_report_raw.md",  # token, credentials, OPENNEBULA_PASSWORD
        "tnlcm_report_summary.json",  # bloque credentials + token de InfluxDB
    }
)
SECRET_SUFFIXES = (".conf",)


class InvalidExecutionIdError(ValueError):
    """El identificador no puede usarse como componente de ruta."""


class ExecutionArtifactsNotFoundError(FileNotFoundError):
    """La ejecucion no tiene carpeta de artefactos que empaquetar."""


def validate_execution_id(execution_id: str) -> str:
    """Devuelve el identificador si es utilizable como nombre de directorio.

    Levanta `InvalidExecutionIdError` en cualquier otro caso. Es lo primero que
    hace el endpoint de descarga, antes de construir ninguna ruta.
    """
    if not EXECUTION_ID_PATTERN.match(execution_id or ""):
        raise InvalidExecutionIdError(
            "execution_id must match ^[A-Za-z0-9_-]{1,64}$ to be used as a path"
        )
    return execution_id


def resolve_execution_dir(root: str | Path, execution_id: str) -> Path:
    """Carpeta de artefactos de la ejecucion, garantizando que cae dentro de `root`.

    La contencion se comprueba sobre la ruta YA resuelta, no sobre el texto: un
    enlace simbolico dentro de `artifacts/` apuntando afuera pasaria cualquier
    comprobacion sintactica y no pasa esta.
    """
    validate_execution_id(execution_id)

    base = Path(root).resolve()
    target = (base / execution_id).resolve()
    if target != base and base not in target.parents:
        raise InvalidExecutionIdError(f"resolved path escapes the artifacts root: {execution_id}")
    if not target.is_dir():
        raise ExecutionArtifactsNotFoundError(f"no artifacts directory for '{execution_id}'")
    return target


def is_secret_file(path: Path) -> bool:
    """True si el fichero lleva claves de acceso y no debe salir por defecto."""
    return path.name in SECRET_FILENAMES or path.suffix.lower() in SECRET_SUFFIXES


def build_execution_zip(
    execution_dir: str | Path,
    *,
    include_secrets: bool = False,
    readme: str | None = None,
) -> bytes:
    """Empaqueta la carpeta de una ejecucion y devuelve los bytes del ZIP.

    Se construye en memoria a proposito: las carpetas reales rondan los 100 KB,
    asi que no compensa un temporal en disco que ademas habria que limpiar.

    Las rutas dentro del ZIP son RELATIVAS a la carpeta de la ejecucion, de modo
    que al descomprimir no se recree el arbol `artifacts/<id>/` entero.
    """
    root = Path(execution_dir)
    buffer = io.BytesIO()
    skipped: list[str] = []

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if readme is not None:
            archive.writestr(README_NAME, readme)

        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if not include_secrets and is_secret_file(path):
                skipped.append(path.name)
                continue
            archive.write(path, arcname=path.relative_to(root).as_posix())

    if skipped:
        logger.info(
            "Bundle for %s omits %d secret-bearing file(s): %s",
            root.name,
            len(skipped),
            ", ".join(sorted(skipped)),
        )

    return buffer.getvalue()
