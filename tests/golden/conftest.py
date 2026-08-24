"""Andamiaje del nivel `golden`: comparar la salida generada contra una copia congelada.

Un golden test fija el resultado EXACTO de una transformacion determinista. No
afirma que la salida sea correcta -eso lo decide quien revisa el fichero la
primera vez-, afirma que no ha cambiado sin que nadie se entere. Por eso el
fichero esperado se lee como codigo en la revision: un golden aceptado a ciegas
congela un bug con la misma fidelidad que el comportamiento bueno.

Que hace deterministas a estos casos (verificado sobre artefactos reales):
el contenido generado no lleva `execution_id`, ni timestamps, ni rutas
absolutas. El `execution_id` solo aparece en la RUTA de salida y, en los
TestCases de dataset, como el literal `"@{ExecutionId}"`, que es un token del
Expander de ELCM y se resuelve en su runtime, no aqui.

El unico vector de no-determinismo real es la version del binario `ytt`, fijada
a v0.55.1 en CI y en CLAUDE.md 5.

Regenerar tras un cambio intencionado:

    GOLDEN_UPDATE=1 python -m pytest -m golden

y despues revisar el diff fichero a fichero antes de commitear.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Callable

import pytest

# Raiz de las salidas congeladas. Un subdirectorio por caso.
GOLDEN_DIR = Path(__file__).parent / "expected"

# Lo reescrito en esta sesion, para avisar al final: en modo actualizacion los
# tests pasan sin comprobar nada y eso no puede pasar desapercibido.
_REGENERATED: list[str] = []


def _updating() -> bool:
    """Si toca reescribir los esperados en vez de compararlos.

    Se niega a hacerlo en CI: alli un `GOLDEN_UPDATE` colado en el entorno
    convertiria la puerta de regresion en un sello de goma que aprueba
    cualquier cambio.
    """
    if os.environ.get("GOLDEN_UPDATE") != "1":
        return False
    if os.environ.get("CI"):
        raise RuntimeError(
            "GOLDEN_UPDATE=1 en CI: los esperados se regeneran en local y se revisan "
            "en el diff, nunca automaticamente en el pipeline."
        )
    return True


def _normalize(text: str) -> str:
    """Finales de linea a `\n`.

    Se desarrolla en Windows y CI corre en Linux. Sin esto el golden fallaria
    por CRLF y no por una regresion real, que es justo el ruido que hace que la
    gente deje de mirar los fallos.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _assert_matches_golden(actual: str, relative: str) -> None:
    golden_path = GOLDEN_DIR / relative
    actual = _normalize(actual)

    if _updating():
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8", newline="\n")
        _REGENERATED.append(relative)
        # Se escribe y se sigue, sin `pytest.skip`: un test puede congelar varios
        # ficheros y saltar aqui abortaria el resto.
        return

    if not golden_path.is_file():
        raise AssertionError(
            f"no existe el golden '{relative}'. Generalo con:\n"
            f"    GOLDEN_UPDATE=1 python -m pytest -m golden\n"
            f"y revisa el fichero antes de commitearlo."
        )

    expected = _normalize(golden_path.read_text(encoding="utf-8"))
    if actual == expected:
        return

    # Un `assert a == b` sobre 2 KB de YAML es ilegible; el diff senala la linea.
    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile=f"esperado/{relative}",
            tofile=f"generado/{relative}",
            lineterm="",
        )
    )
    raise AssertionError(
        f"la salida generada ya no coincide con '{relative}'.\n\n{diff}\n\n"
        "Si el cambio es intencionado, regenera con GOLDEN_UPDATE=1 y revisa el diff."
    )


@pytest.fixture
def golden() -> Callable[[str, str], None]:
    """Compara un contenido generado contra `expected/<relative>`, byte a byte.

    Se expone como fixture y no como funcion importable porque `tests/` no es un
    paquete (no hay ningun `__init__.py` en la suite), asi que un
    `from .conftest import ...` no resolveria.

    Uso:
        def test_algo(golden):
            golden(contenido_generado, "caso/fichero.yaml")
    """
    return _assert_matches_golden


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Avisar de que esta sesion NO ha comprobado nada, si se regeneraron esperados."""
    if not _REGENERATED:
        return
    terminalreporter.section("golden", sep="=", yellow=True, bold=True)
    terminalreporter.write_line(
        f"{len(_REGENERATED)} esperados REGENERADOS (no se comprobo ninguna regresion):"
    )
    for relative in _REGENERATED:
        terminalreporter.write_line(f"  {relative}")
    terminalreporter.write_line("Revisa el diff de cada uno antes de commitear.")
