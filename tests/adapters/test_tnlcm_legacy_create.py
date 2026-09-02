"""Despliegue de una TN: crear (endpoint legacy), activar y reconciliar.

Los tres verbos van a rutas distintas, asi que las respuestas se declaran por
metodo y las afirmaciones miran que se pidio realmente por el cable:

    POST /api/v1/trial-network/legacy              crear
    PUT  /api/v1/trial-networks/{tn_id}/activate   activar
    GET  /api/v1/trial-networks/{tn_id}            estado (reconciliacion)

La reconciliacion es lo delicado: TNLCM devuelve 400 tanto si el descriptor es
invalido como si la TN ya existe, asi que el estado real es lo unico que
distingue "reanudar" de "fallar".
"""

import pytest

from app.adapters import tnlcm
from app.domain.descriptor import InfrastructureConfig

CREATE_PATH = "/api/v1/trial-network/legacy"
ACTIVATE_PATH = "/api/v1/trial-networks/tn-demo/activate"
STATUS_PATH = "/api/v1/trial-networks/tn-demo"

# Ventana que TNLCM necesita para registrar la TN antes de poder activarla.
REGISTRATION_WINDOW_SECONDS = 20


@pytest.fixture
def infra() -> InfrastructureConfig:
    return InfrastructureConfig(name="tn-demo", descriptor_path="desc.yaml", parameters={})


@pytest.fixture(autouse=True)
def _stub_multipart(monkeypatch):
    """El cuerpo multipart se construye leyendo el descriptor del disco.

    Aqui interesa el flujo de despliegue, no el empaquetado del fichero, que
    tiene sus propias pruebas.

    El doble replica la firma completa: el adaptador llama con `descriptor_path`
    y un stub que solo aceptara `infra` fallaria con TypeError.
    """
    monkeypatch.setattr(
        tnlcm,
        "_legacy_multipart_from_infra",
        lambda infra, descriptor_path=None: ({}, {}),
    )


def _create_ok(fake_http):
    fake_http.respond(201, json={"tn_id": "tn-demo"}, when=CREATE_PATH, method="POST")


def _create_already_exists(fake_http):
    fake_http.respond(
        400, json={"message": "trial network already exists"}, when=CREATE_PATH, method="POST"
    )


def _state_is(fake_http, state: str):
    fake_http.respond(
        200, json={"tn_id": "tn-demo", "state": state}, when=STATUS_PATH, method="GET"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "backend_message"),
    [
        (400, "estado no permitido"),
        (404, "recursos no encontrados"),
        (422, "descriptor invalido"),
    ],
)
async def test_create_client_errors_are_not_retried(
    fake_http, tnlcm_token, sleepless, infra, status_code, backend_message
):
    fake_http.respond(
        status_code, json={"message": backend_message}, when=CREATE_PATH, method="POST"
    )
    # La TN no existe: un 400 real de create debe seguir fallando.
    fake_http.respond(404, json={"message": "not found"}, when=STATUS_PATH, method="GET")

    with pytest.raises(RuntimeError) as exc_info:
        await tnlcm.deploy_trial_network(infra)

    message = str(exc_info.value)
    assert backend_message in message
    assert tnlcm.TNLCM_LEGACY_ERROR_HINT in message
    assert fake_http.paths_for("POST") == [CREATE_PATH]
    assert fake_http.paths_for("PUT") == []


@pytest.mark.asyncio
async def test_create_201_waits_the_registration_window_and_activates(
    fake_http, tnlcm_token, sleepless, infra
):
    _create_ok(fake_http)
    fake_http.respond(200, json={"status": "ok"}, when=ACTIVATE_PATH, method="PUT")

    assert await tnlcm.deploy_trial_network(infra) == "tn-demo"

    assert fake_http.paths_for("POST") == [CREATE_PATH]
    assert fake_http.paths_for("PUT") == [ACTIVATE_PATH]
    assert sleepless == [REGISTRATION_WINDOW_SECONDS]
    # El token cargado en memoria tiene que viajar en la activacion.
    assert fake_http.requests[-1].headers["Authorization"] == f"Bearer {tnlcm_token}"


@pytest.mark.asyncio
async def test_create_400_on_already_activated_tn_skips_create_and_activate(
    fake_http, tnlcm_token, sleepless, infra
):
    """Un create 400 sobre una TN ya 'activated' no repite nada y no espera."""
    _create_already_exists(fake_http)
    _state_is(fake_http, "activated")

    assert await tnlcm.deploy_trial_network(infra) == "tn-demo"

    assert fake_http.paths_for("POST") == [CREATE_PATH]
    assert fake_http.paths_for("GET") == [STATUS_PATH]
    assert fake_http.paths_for("PUT") == []  # no se re-activa
    assert sleepless == []  # ni se espera la ventana de registro


@pytest.mark.asyncio
async def test_create_400_on_created_tn_skips_create_but_activates(
    fake_http, tnlcm_token, sleepless, infra
):
    """Un create 400 sobre una TN ya 'created' salta el create pero si la activa."""
    _create_already_exists(fake_http)
    _state_is(fake_http, "created")
    fake_http.respond(200, json={"status": "ok"}, when=ACTIVATE_PATH, method="PUT")

    assert await tnlcm.deploy_trial_network(infra) == "tn-demo"

    assert fake_http.paths_for("POST") == [CREATE_PATH]
    assert fake_http.paths_for("GET") == [STATUS_PATH]
    assert fake_http.paths_for("PUT") == [ACTIVATE_PATH]
    assert sleepless == []  # TN ya registrada: sin ventana de espera


@pytest.mark.asyncio
async def test_activate_400_is_tolerated_when_state_confirms_activated(
    fake_http, tnlcm_token, sleepless, infra
):
    """Un 400 al activar se tolera solo si el estado real confirma 'activated'."""
    _create_ok(fake_http)
    fake_http.respond(
        400,
        json={"message": "trial network already activated"},
        when=ACTIVATE_PATH,
        method="PUT",
    )
    _state_is(fake_http, "activated")

    assert await tnlcm.deploy_trial_network(infra) == "tn-demo"

    assert sleepless == [REGISTRATION_WINDOW_SECONDS]  # create fresco: si espera
    assert fake_http.paths_for("POST") == [CREATE_PATH]
    assert fake_http.paths_for("PUT") == [ACTIVATE_PATH]  # se intenta (da 400)
    assert fake_http.paths_for("GET") == [STATUS_PATH]  # y se confirma el estado


@pytest.mark.asyncio
async def test_create_400_on_terminal_tn_raises_actionable_error(
    fake_http, tnlcm_token, sleepless, infra
):
    """Una TN en estado terminal no se reanuda: hay que destruirla antes."""
    _create_already_exists(fake_http)
    _state_is(fake_http, "failed")

    with pytest.raises(RuntimeError) as exc_info:
        await tnlcm.deploy_trial_network(infra)

    message = str(exc_info.value)
    assert "terminal state" in message
    assert "failed" in message
    assert fake_http.paths_for("PUT") == []  # no se activa una TN terminal
    assert fake_http.paths_for("GET") == [STATUS_PATH]


# ===== Que el que espera se entere de los reintentos =====
#
# Un reintento de activate solo dejaba rastro en `telemetry.log_event`, que NO
# retiene nada en memoria: ningun endpoint podia contarlo, asi que quien esperaba
# en la UI no tenia forma de saber que se estaba reintentando hasta que el
# despliegue fallaba del todo. `on_progress` es lo unico que lo saca de ahi.


def _activate_fails_once_then_succeeds(fake_http):
    """La cola es global y se consume en orden: create, activate, activate."""
    fake_http.respond_once(201, json={"tn_id": "tn-demo"})
    fake_http.respond_once(502, json={"message": "backend caido"})
    fake_http.respond(200, json={"status": "ok"}, when=ACTIVATE_PATH, method="PUT")


@pytest.mark.asyncio
async def test_activate_retries_are_reported_to_the_caller(
    fake_http, tnlcm_token, sleepless, infra
):
    _activate_fails_once_then_succeeds(fake_http)
    avisos: list[str] = []

    assert await tnlcm.deploy_trial_network(infra, on_progress=avisos.append) == "tn-demo"

    assert fake_http.paths_for("PUT") == [ACTIVATE_PATH, ACTIVATE_PATH]
    assert len(avisos) == 1, "una linea por reintento, no por intento"
    assert "attempt 1/3" in avisos[0]
    assert "tn-demo" in avisos[0]


@pytest.mark.asyncio
async def test_the_progress_hook_is_optional(fake_http, tnlcm_token, sleepless, infra):
    """Sin gancho el reintento sigue su curso: es un extra, no una dependencia."""
    _activate_fails_once_then_succeeds(fake_http)

    assert await tnlcm.deploy_trial_network(infra) == "tn-demo"
    assert fake_http.paths_for("PUT") == [ACTIVATE_PATH, ACTIVATE_PATH]


@pytest.mark.asyncio
async def test_a_broken_progress_hook_does_not_break_the_deploy(
    fake_http, tnlcm_token, sleepless, infra
):
    """Informar del progreso no puede tumbar un despliegue que iba a reintentar."""
    _activate_fails_once_then_succeeds(fake_http)

    def _explota(_text: str) -> None:
        raise RuntimeError("la UI se fue")

    assert await tnlcm.deploy_trial_network(infra, on_progress=_explota) == "tn-demo"
