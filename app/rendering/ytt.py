"""Invocacion del binario `ytt` (Carvel).

Home unico del subproceso, para no duplicar su manejo entre los generadores
TNLCM y ELCM. Es el camino de renderizado VIVO del pipeline; la alternativa en
Python puro vive en `app.rendering.python_render` y hoy no la usa nadie.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_ytt_cli(files: list[Path], *, timeout: int = 60) -> str:
    """Invoca el binario `ytt` con los ficheros dados y devuelve su stdout.

    Home único para ejecutar ytt nativo (evita duplicar el manejo del
    subproceso entre los generadores TNLCM y ELCM). Es SÍNCRONA a propósito:
    quien la llame desde un `async def` debe envolverla en
    `await asyncio.to_thread(run_ytt_cli, ...)` para no bloquear el event loop.

    Raises:
        RuntimeError: si el binario `ytt` no está en el PATH, si expira el
            timeout o si ytt termina con código distinto de 0.
    """
    cmd = ["ytt"]
    for f in files:
        cmd.extend(["-f", str(f)])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError("YTT binary not found in PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"YTT timed out after {timeout} seconds.") from exc

    if result.returncode != 0:
        raise RuntimeError(f"YTT failed with code {result.returncode}: {result.stderr}")

    return result.stdout
