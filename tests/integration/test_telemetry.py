"""Escritura de la telemetria en disco y formato de sus eventos.

Es de integracion, no unitaria: comprueba el fichero `telemetry.log` que el
singleton deja realmente escrito bajo `artifacts/`, no solo el objeto en memoria.

El aislamiento del directorio lo pone la fixture `isolate_artifacts_dir`; antes
cada prueba repetia a mano el mismo `try/finally` sobre `settings.artifacts_dir`.
"""

import json
import logging
import re
from pathlib import Path

import pytest

from app.core.config import settings
from app.observability.telemetry import telemetry

pytestmark = pytest.mark.usefixtures("isolate_artifacts_dir")

# Formato humano HH:MM:SS-DD/MM/AAAA que acompana a la marca ISO.
TIMESTAMP_PATTERN = re.compile(r"^\d{2}:\d{2}:\d{2}-\d{2}/\d{2}/\d{4}$")


def _telemetry_log(execution_id: str | None = None) -> Path:
    """Ruta del `telemetry.log`; con `execution_id`, el de esa ejecucion."""
    root = Path(settings.artifacts_dir) / "tests"
    return root / execution_id / "telemetry.log" if execution_id else root / "telemetry.log"


def _last_event(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])


def test_telemetry_appends_to_single_file() -> None:
    execution_id = "exec-telemetry-file"
    telemetry.log_event(
        "info",
        "unit.test.event",
        service="test",
        operation="write",
        execution_id=execution_id,
    )

    path = _telemetry_log(execution_id)
    assert path.exists()

    last = _last_event(path)
    assert last.get("message") == "unit.test.event"
    assert last.get("service") == "test"
    # Marca ISO ademas de la humana: es la que permite ordenar los eventos.
    assert last.get("ts", "").startswith("20")


def test_timer_stop_with_error_status_emits_a_failed_message() -> None:
    """Un paso fallido no puede anunciarse como `.completed`."""
    execution_id = "exec-timer-failed"
    timer = telemetry.start_timer("orchestrator", "tnlcm_phase", execution_id)
    timer.start()
    timer.stop(status="error")

    last = _last_event(_telemetry_log(execution_id))
    assert last["message"] == "orchestrator.tnlcm_phase.failed"
    assert last["phase"] == "error"
    assert last["status"] == "error"


def test_start_timer_without_execution_id_creates_no_artifact_directory() -> None:
    """Regresion: acunar un UUID por medida dejaba cientos de carpetas huerfanas."""
    timer = telemetry.start_timer("tnlcm", "destroy")
    timer.start()
    timer.stop(status="success")

    root = Path(settings.artifacts_dir) / "tests"
    assert [child.name for child in root.iterdir() if child.is_dir()] == []
    assert (root / "telemetry.log").exists()


def test_log_event_uses_the_human_timestamp_format(caplog) -> None:
    """El evento debe llevar `timestamp` en HH:MM:SS-DD/MM/AAAA.

    La version anterior encerraba los asserts en un `for/try/except: pass`, asi
    que pasaba en verde tambien cuando no encontraba ningun registro. Aqui la
    busqueda se separa de la comprobacion y se exige haber encontrado el evento.
    """
    with caplog.at_level(logging.INFO, logger="telemetry"):
        telemetry.log_event(
            "info",
            "test_event",
            service="test_service",
            operation="test_op",
            execution_id="exec-timestamp",
        )

    payloads = []
    for record in caplog.records:
        try:
            payloads.append(json.loads(record.message))
        except json.JSONDecodeError:
            continue

    matching = [p for p in payloads if p.get("message") == "test_event"]
    assert matching, f"no se emitio el evento; payloads vistos: {payloads}"
    assert TIMESTAMP_PATTERN.match(matching[-1]["timestamp"])
