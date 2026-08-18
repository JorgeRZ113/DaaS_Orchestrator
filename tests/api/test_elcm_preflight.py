"""Tests del pre-flight ELCM: el body se valida ANTES de desplegar nada.

Todo lo que este modulo detecta se detectaba antes dentro de la fase ELCM, que
corre en segundo plano y sobre una TN ya desplegada: el fallo llegaba tarde,
dejaba un experimento FAILED y quemaba su nombre. Aqui se comprueba que sale
como error de validacion y con la lista completa de problemas.
"""

import pytest
from fastapi.testclient import TestClient

from app.services import preflight
from app.core.config import settings
from app.main import app
from app.domain.descriptor import DatasetRequest, ExperimentConfig
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord
from app.services import state
from app.services import background
from app.services.phases import elcm as elcm_phase

client = TestClient(app)


def _headers() -> dict[str, str]:
    return {"x-api-key": settings.api_key}


def _validate(
    testcase_paths: list[str],
    ues_paths: list[str] | None = None,
    output: list[str] | None = None,
) -> None:
    preflight.validate_elcm_request(
        ExperimentConfig(
            name="exp-preflight",
            testcase_paths=testcase_paths,
            ues_paths=ues_paths or [],
        ),
        DatasetRequest(output=output or ["logs"]),
    )


def _problems(*args, **kwargs) -> list[str]:
    with pytest.raises(preflight.ElcmPreflightError) as exc_info:
        _validate(*args, **kwargs)
    return exc_info.value.problems


def _write(path, content: str) -> str:
    path.write_text(content, encoding="utf-8")
    return str(path)


VALID_TESTCASE = """
Name: "TC_Valid"
Version: 2
Sequence:
  - Order: 0
    Task: "Run.Dummy"
"""

VALID_UE = """
UE_Preflight:
  - Order: 0
    Task: "Run.Publish"
    Config:
      SutIp: "10.0.0.1"
"""


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------


def test_valid_experiment_passes() -> None:
    _validate(["TC_ping.yml"], ues_paths=["UE_Variables.yml"])


def test_empty_ue_reference_is_ignored() -> None:
    # Mismo criterio que la fase ELCM, que salta las referencias vacias.
    _validate(["TC_ping.yml"], ues_paths=[""])


# ---------------------------------------------------------------------------
# Ficheros que no existen
# ---------------------------------------------------------------------------


def test_missing_testcase_is_reported() -> None:
    problems = _problems(["PrometheusToInflux_Capture"])

    assert len(problems) == 1
    assert "PrometheusToInflux_Capture" in problems[0]
    assert "file not found" in problems[0]


def test_missing_ue_is_reported() -> None:
    problems = _problems(["TC_ping.yml"], ues_paths=["UE_inexistente.yml"])

    assert any(
        "UE_inexistente.yml" in problem and "file not found" in problem for problem in problems
    )


def test_empty_testcase_list_is_reported() -> None:
    problems = _problems([])

    assert any("at least one TestCase" in problem for problem in problems)


# ---------------------------------------------------------------------------
# Contrato de los TestCases (ELCM los registra por su Name y exige Version 2)
# ---------------------------------------------------------------------------


def test_testcase_without_version_2_is_reported(tmp_path) -> None:
    path = _write(tmp_path / "TC_NoVersion.yml", 'Name: "TC_NoVersion"\nSequence: []\n')

    problems = _problems([path])

    assert any("'Version: 2'" in problem for problem in problems)


def test_testcase_without_name_is_reported(tmp_path) -> None:
    path = _write(tmp_path / "TC_NoName.yml", "Version: 2\nSequence: []\n")

    problems = _problems([path])

    assert any("has no 'Name'" in problem for problem in problems)


def test_duplicate_testcase_name_is_reported(tmp_path) -> None:
    first = _write(tmp_path / "TC_A.yml", VALID_TESTCASE)
    second = _write(tmp_path / "TC_B.yml", VALID_TESTCASE)

    problems = _problems([first, second])

    assert any("same Name 'TC_Valid'" in problem for problem in problems)


def test_unparseable_testcase_is_reported(tmp_path) -> None:
    path = _write(tmp_path / "TC_Broken.yml", "Name: [unclosed\n")

    problems = _problems([path])

    assert any("not readable YAML" in problem for problem in problems)


# ---------------------------------------------------------------------------
# Contrato de los UEs (lista de acciones V1, sin Name/Version)
# ---------------------------------------------------------------------------


def test_ue_with_testcase_syntax_is_reported(tmp_path) -> None:
    path = _write(tmp_path / "UE_AsTestCase.yml", VALID_TESTCASE)

    problems = _problems(["TC_ping.yml"], ues_paths=[path])

    assert any("TestCase V2 syntax" in problem for problem in problems)


def test_ue_action_without_order_is_reported(tmp_path) -> None:
    path = _write(tmp_path / "UE_NoOrder.yml", 'UE_X:\n  - Task: "Run.Publish"\n')

    problems = _problems(["TC_ping.yml"], ues_paths=[path])

    assert any("no 'Order'" in problem for problem in problems)


# ---------------------------------------------------------------------------
# Dataset: la entrega pedida tiene que poder producirse
# ---------------------------------------------------------------------------


def test_dashboard_without_capture_testcase_is_reported() -> None:
    problems = _problems(["TC_ping.yml"], output=["dashboard"])

    assert any("capture TestCase" in problem for problem in problems)


def test_dashboard_with_capture_testcase_passes() -> None:
    _validate(["TestCase_prometheus_capture.yml"], output=["dashboard"])


def test_unimplemented_output_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(elcm_phase, "IMPLEMENTED_DATASET_OUTPUTS", {"logs"})

    problems = _problems(["TC_ping.yml"], output=["csv"])

    assert any("not implemented yet" in problem for problem in problems)


# ---------------------------------------------------------------------------
# Todos los problemas salen juntos, no solo el primero
# ---------------------------------------------------------------------------


def test_all_problems_are_collected(tmp_path) -> None:
    no_name = _write(tmp_path / "TC_NoName.yml", "Version: 2\nSequence: []\n")

    problems = _problems([no_name, "no_existe.yml"], ues_paths=["tampoco_existe.yml"])

    assert len(problems) == 3


# ---------------------------------------------------------------------------
# Integracion: el endpoint responde 400 y no toca el estado de la ejecucion
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_executions(monkeypatch):
    monkeypatch.setattr(state, "executions", {})
    monkeypatch.setattr(state, "save_to_disk", lambda: None)

    def _fake_spawn(coro, *, name: str):
        coro.close()
        raise AssertionError("an invalid body must not spawn the ELCM phase")

    monkeypatch.setattr(background, "spawn_background_task", _fake_spawn)


def test_elcm_endpoint_rejects_invalid_body_without_touching_the_record(
    isolated_executions,
) -> None:
    state.executions["tn-a"] = ExecutionRecord(
        execution_id="tn-a", status=ExecutionState.tn_ready, tn_id="tn-a"
    )
    body = {
        "experiment": {
            "name": "exp-typo",
            "testcase_paths": ["PrometheusToInflux_Capture"],
            "ues_paths": [],
        }
    }

    response = client.post("/executions/tn-a/elcm?wait=false", json=body, headers=_headers())

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert any("PrometheusToInflux_Capture" in problem for problem in detail["invalid_experiment"])

    # El nombre del experimento no se quema y la TN sigue lista: basta corregir
    # el body y reenviar.
    record = state.executions["tn-a"]
    assert record.experiments == []
    assert record.status is ExecutionState.tn_ready


def test_executions_endpoint_rejects_invalid_experiment_before_deploying(
    isolated_executions, monkeypatch
) -> None:
    async def _should_not_run(descriptor, source=None):
        raise AssertionError("an invalid body must not create a TNLCM execution")

    monkeypatch.setattr("app.services.orchestrator.create_tnlcm_execution", _should_not_run)

    payload = {
        "infrastructure": {"name": "tn-preflight"},
        "experiment": {
            "name": "exp-typo",
            "testcase_paths": ["TC_ping.yml"],
            "ues_paths": ["UE_inexistente.yml"],
        },
    }

    response = client.post("/executions?wait=false", json=payload, headers=_headers())

    assert response.status_code == 400
    assert "invalid_experiment" in response.json()["detail"]
