"""GET /executions/{id}/summary: las dos representaciones y la ejecucion ausente.

Solo cubre la capa HTTP; la construccion del resumen se prueba en
`tests/unit/test_execution_summary.py`. El registro se coloca directamente en
`state.executions` porque el endpoint lo lee de ahi via `orchestrator.get_execution`.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.domain.execution import ExecutionRecord
from app.domain.enums import ExecutionState
from app.main import app
from app.observability.telemetry import telemetry
from app.services import state

client = TestClient(app)

pytestmark = pytest.mark.usefixtures("isolate_orchestrator_state")


@pytest.fixture(autouse=True)
def clean_telemetry():
    """El singleton es global al proceso: hay que aislarlo entre tests."""
    telemetry.reset()
    yield
    telemetry.reset()


def _headers() -> dict[str, str]:
    return {"x-api-key": settings.api_key}


def _observe(execution_id: str, service: str, operation: str, seconds: float) -> None:
    telemetry.observe_duration(
        service=service,
        operation=operation,
        execution_id=execution_id,
        duration_seconds=seconds,
        status="success",
    )


def _record(execution_id: str) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=execution_id,
        status=ExecutionState.tn_ready,
        tn_id=execution_id,
    )


def test_summary_endpoint_returns_steps() -> None:
    _observe("exec-api", "tnlcm", "activate", 237.36)
    state.executions["exec-api"] = _record("exec-api")

    response = client.get("/executions/exec-api/summary", headers=_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["execution_id"] == "exec-api"
    assert payload["network"] == "exec-api"
    assert payload["state"] == "TN_READY"
    assert any(
        step["step"] == "Starting up the virtual machines" and step["duration"] == "3 min 57 s"
        for step in payload["steps"]
    )


def test_summary_endpoint_can_return_markdown() -> None:
    _observe("exec-api-md", "tnlcm", "activate", 237.36)
    state.executions["exec-api-md"] = _record("exec-api-md")

    response = client.get("/executions/exec-api-md/summary?format=markdown", headers=_headers())

    assert response.status_code == 200
    assert response.text.startswith("# Execution summary — exec-api-md")


def test_summary_endpoint_returns_404_for_unknown_execution() -> None:
    response = client.get("/executions/does-not-exist/summary", headers=_headers())

    assert response.status_code == 404
