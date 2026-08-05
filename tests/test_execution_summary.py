"""Tests de la capa de resumen legible para experimentadores."""

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app.config import settings
from app.main import app
from app.models import ExecutionRecord, ExecutionState, ExperimentRun
from app.utils.execution_summary import (
    build_execution_summary,
    format_duration_human,
    render_summary_markdown,
)
from app.utils.telemetry import telemetry

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_telemetry():
    """El singleton es global al proceso: hay que aislarlo entre tests."""
    telemetry.reset()
    yield
    telemetry.reset()


def _headers() -> dict[str, str]:
    return {"x-api-key": settings.api_key}


def _record(execution_id: str, status=ExecutionState.tn_ready, **kwargs) -> ExecutionRecord:
    return ExecutionRecord(execution_id=execution_id, status=status, **kwargs)


def _observe(execution_id, service, operation, seconds, status="success") -> None:
    telemetry.observe_duration(
        service=service,
        operation=operation,
        execution_id=execution_id,
        duration_seconds=seconds,
        status=status,
    )


def _step(summary: dict, label: str) -> dict:
    for step in [*summary["steps"], *summary["technical_steps"]]:
        if step["step"] == label:
            return step
    raise AssertionError(f"Step not found: {label}")


# ---------------------------------------------------------------------------
# format_duration_human
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0.4, "< 1 s"),
        (7.5279, "7.5 s"),
        (237.355, "3 min 57 s"),
        (3930.0, "1 h 05 min"),
    ],
)
def test_format_duration_human_covers_all_ranges(seconds, expected) -> None:
    assert format_duration_human(seconds) == expected


def test_format_duration_human_returns_none_without_value() -> None:
    assert format_duration_human(None) is None


# ---------------------------------------------------------------------------
# build_execution_summary
# ---------------------------------------------------------------------------


def test_summary_only_includes_steps_of_its_own_execution() -> None:
    """Regresion: el singleton nunca se resetea, el informe arrastraba medidas ajenas."""
    _observe("exec-a", "tnlcm", "activate", 120.0)
    _observe("exec-b", "tnlcm", "activate", 999.0)
    _observe("exec-b", "tnlcm", "destroy", 30.0)

    summary = build_execution_summary("exec-a", _record("exec-a"))

    assert _step(summary, "Starting up the virtual machines")["duration_seconds"] == 120.0
    assert _step(summary, "Releasing the network")["status"] == "pending"
    assert summary["total_duration_seconds"] == 120.0


def test_failed_step_is_reported_with_a_human_message() -> None:
    _observe("exec-fail", "tnlcm", "create", 5.0)
    _observe("exec-fail", "tnlcm", "activate", 190.0, status="error")
    record = _record(
        "exec-fail",
        status=ExecutionState.failed,
        error="TNLCM activate timeout after 3 attempts",
    )

    summary = build_execution_summary("exec-fail", record)

    assert summary["outcome"] == "error"
    assert summary["status"] == "Failed"
    assert _step(summary, "Starting up the virtual machines")["status"] == "error"
    assert "Suggestion:" in summary["what_went_wrong"]
    assert "❌" in render_summary_markdown(summary)


def test_unknown_error_is_kept_verbatim() -> None:
    record = _record("exec-x", status=ExecutionState.failed, error="Something odd happened")

    summary = build_execution_summary("exec-x", record)

    assert summary["what_went_wrong"] == "Something odd happened"


def test_steps_after_a_terminal_failure_are_skipped_not_pending() -> None:
    record = _record("exec-term", status=ExecutionState.failed, error="boom")

    summary = build_execution_summary("exec-term", record)

    assert {step["status"] for step in summary["steps"]} == {"skipped"}


def test_step_in_flight_is_reported_as_running() -> None:
    record = _record("exec-run", status=ExecutionState.deploying)

    summary = build_execution_summary("exec-run", record)

    assert _step(summary, "Starting up the virtual machines")["status"] == "running"
    assert _step(summary, "Releasing the network")["status"] == "pending"
    assert summary["outcome"] == "running"


def test_tn_ready_with_error_does_not_claim_success() -> None:
    """TN_READY es tambien el estado final de un experimento fallido."""
    record = _record("exec-amb", status=ExecutionState.tn_ready, error="Experiment failed")

    summary = build_execution_summary("exec-amb", record)

    assert summary["outcome"] == "error"
    assert summary["status"] == "Network ready, but the last experiment failed"


def test_experiments_get_one_row_each_with_their_name() -> None:
    _observe("exec-exp", "orchestrator", "elcm_phase", 32.9)
    record = _record(
        "exec-exp",
        experiments=[
            ExperimentRun(name="exp-demo", status="FINISHED"),
            ExperimentRun(name="exp-two", status="FAILED", error="ELCM logs not found"),
        ],
    )

    summary = build_execution_summary("exec-exp", record)

    assert _step(summary, 'Experiment "exp-demo"')["duration"] == "32.9 s"
    failed = _step(summary, 'Experiment "exp-two"')
    assert failed["status"] == "error"
    assert "Suggestion:" in failed["detail"]
    assert summary["experiments_total"] == 2
    assert summary["experiments_successful"] == 1


def test_experiment_duration_falls_back_to_its_timestamps() -> None:
    record = _record(
        "exec-ts",
        experiments=[
            ExperimentRun(
                name="exp-demo",
                status="FINISHED",
                started_at="2026-07-27T11:45:19.826065+00:00",
                finished_at="2026-07-27T11:46:03.431927+00:00",
            )
        ],
    )

    summary = build_execution_summary("exec-ts", record)

    assert _step(summary, 'Experiment "exp-demo"')["duration"] == "43.6 s"


def test_technical_steps_keep_uncatalogued_measurements() -> None:
    _observe("exec-tech", "elcm", "upload", 0.4)
    _observe("exec-tech", "brandnew", "thing", 2.0)

    summary = build_execution_summary("exec-tech", _record("exec-tech"))

    labels = [step["step"] for step in summary["technical_steps"]]
    assert "Uploading test cases" in labels
    assert "brandnew/thing" in labels
    assert all(step["step"] != "Uploading test cases" for step in summary["steps"])


def test_rollup_measurements_are_not_shown_as_steps() -> None:
    _observe("exec-roll", "orchestrator", "execution_total", 387.29)
    _observe("exec-roll", "orchestrator", "tnlcm_phase", 273.2)
    _observe("exec-roll", "tnlcm", "activate", 237.35)

    summary = build_execution_summary("exec-roll", _record("exec-roll"))

    labels = [step["step"] for step in [*summary["steps"], *summary["technical_steps"]]]
    assert "orchestrator/tnlcm_phase" not in labels
    assert "orchestrator/execution_total" not in labels
    # El total sale del timer global, no de la suma de los pasos visibles.
    assert summary["total_duration_seconds"] == 387.29
    assert summary["total_duration"] == "6 min 27 s"


def test_retries_are_aggregated_with_the_attempt_count() -> None:
    _observe("exec-retry", "tnlcm", "activate", 10.0, status="error")
    _observe("exec-retry", "tnlcm", "activate", 20.0)

    step = _step(
        build_execution_summary("exec-retry", _record("exec-retry")),
        "Starting up the virtual machines",
    )

    assert step["attempts"] == 2
    assert step["duration_seconds"] == 30.0
    assert step["status"] == "ok"


# ---------------------------------------------------------------------------
# render_summary_markdown
# ---------------------------------------------------------------------------


def test_markdown_report_is_readable_end_to_end() -> None:
    _observe("exec-md", "tnlcm", "create", 7.53)
    _observe("exec-md", "tnlcm", "activate", 237.36)
    _observe("exec-md", "wireguard", "tunnel_up", 8.0)
    _observe("exec-md", "orchestrator", "elcm_phase", 32.91)
    _observe("exec-md", "elcm", "upload", 0.4)
    record = _record(
        "exec-md",
        status=ExecutionState.destroyed,
        tn_id="exec-md",
        experiments=[ExperimentRun(name="exp-demo", status="FINISHED")],
        artifacts=["./artifacts/exec-md/result/exp-demo/logs.json"],
    )

    markdown = render_summary_markdown(build_execution_summary("exec-md", record))

    assert markdown.startswith("# Execution summary — exec-md")
    assert "| Creating the network in TNLCM | 7.5 s |" in markdown
    assert "| Starting up the virtual machines | 3 min 57 s |" in markdown
    assert "| Opening the VPN tunnel | 8.0 s |" in markdown
    assert '| Experiment "exp-demo" | 32.9 s |' in markdown
    assert "./artifacts/exec-md/result/exp-demo" in markdown
    assert "<details><summary>Technical detail</summary>" in markdown
    # El vocabulario interno se queda en el canal tecnico.
    assert "elcm_phase" not in markdown
    assert "duration_display" not in markdown
    assert "operation" not in markdown


# ---------------------------------------------------------------------------
# GET /executions/{id}/summary
# ---------------------------------------------------------------------------


def test_summary_endpoint_returns_steps(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "executions", {})
    _observe("exec-api", "tnlcm", "activate", 237.36)
    orchestrator.executions["exec-api"] = _record("exec-api", tn_id="exec-api")

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


def test_summary_endpoint_can_return_markdown(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "executions", {})
    _observe("exec-api-md", "tnlcm", "activate", 237.36)
    orchestrator.executions["exec-api-md"] = _record("exec-api-md", tn_id="exec-api-md")

    response = client.get("/executions/exec-api-md/summary?format=markdown", headers=_headers())

    assert response.status_code == 200
    assert response.text.startswith("# Execution summary — exec-api-md")


def test_summary_endpoint_returns_404_for_unknown_execution(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "executions", {})

    response = client.get("/executions/does-not-exist/summary", headers=_headers())

    assert response.status_code == 404
