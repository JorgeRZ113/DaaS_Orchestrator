"""El formulario reacciona a lo que se elige, y las dos pestanas conviven.

Estas pruebas ejecutan `ui/streamlit_app.py` de verdad con `AppTest`, el harness
que trae Streamlit: no comprueban funciones puras (de eso va
`test_ui_descriptor.py`) sino el CABLEADO, que es lo que no se puede razonar a
mano. Dos cosas concretas:

- que al cambiar `dataset.output` o los componentes elegidos cambien los campos
  que se pintan, cosa que solo funciona porque los formularios ya no viven en un
  `st.form` (dentro de uno ningun widget dispara rerun hasta enviar);
- que las dos pestanas no choquen. `st.tabs` ejecuta el cuerpo de todas en cada
  pasada y ambas pintan `experiment.name`, `dataset.output` y las variables de
  `dataset`; sin el `form_id` que las separaba, la unica cosa que evita un
  `StreamlitDuplicateElementId` son las `key=` con prefijo de pestana. Aqui se
  ve, porque un id duplicado revienta el `at.run()`.

Se saltan si no hay Streamlit: es dependencia opcional (`pip install -e ".[ui]"`)
y el CI instala solo `.[dev]`.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

from app import client as api_client

pytest.importorskip("streamlit", reason="la UI es una dependencia opcional")

from streamlit.testing.v1 import AppTest  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
UI_DIR = REPO / "ui"
APP = UI_DIR / "streamlit_app.py"

# `ui/` es un directorio de scripts, no un paquete: `streamlit run` lo pone en
# sys.path por si mismo y por eso `streamlit_app` importa `descriptor` plano.
sys.path.insert(0, str(UI_DIR))

pytestmark = pytest.mark.skipif(not APP.is_file(), reason="la UI no esta en el arbol")

# Prefijo de los widgets de la pestana de nueva ejecucion (`DESCRIPTOR_KEY`).
NEW = "descriptor_yaml"
ELCM = "elcm_descriptor_yaml"


@pytest.fixture
def app() -> AppTest:
    """La app recien arrancada. Un `at.run()` limpio por prueba: hay estado."""
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [str(exc.value) for exc in at.exception]
    return at


def _dataset_variables(at: AppTest, prefix: str) -> set[str]:
    """Nombres de las variables globales que se estan pintando."""
    widgets = [*at.text_input, *at.number_input]
    marker = f"{prefix}_var_"
    return {w.key[len(marker) :] for w in widgets if w.key and w.key.startswith(marker)}


def _component_inputs(at: AppTest, component: str, prefix: str = NEW) -> set[str]:
    """Campos de un componente que tienen input a la vista, como `seccion_campo`."""
    marker = f"{prefix}_field_{component}_"
    return {w.key[len(marker) :] for w in at.text_input if w.key and w.key.startswith(marker)}


def test_both_tabs_render_without_duplicate_widget_ids(app: AppTest) -> None:
    """La regresion que acecha al haber quitado los `st.form`."""
    assert not app.exception


@pytest.mark.parametrize(
    "outputs,expected",
    [
        (["logs"], set()),
        (["files"], set()),
        (["csv"], {"measurement", "influx_host", "influx_port", "influx_bucket"}),
        (["dashboard"], {"measurement", "panel_interval"}),
        (["raw"], {"measurement", "influx_bucket"}),
        (["logs", "dashboard"], {"measurement", "panel_interval"}),
    ],
)
def test_dataset_variables_follow_the_chosen_output(
    app: AppTest, outputs: list[str], expected: set[str]
) -> None:
    """Solo se ofrecen las variables cuyo modo dueno esta pedido.

    Es lo que evita que el formulario invite a un 422: el servidor rechaza una
    variable si su modo no esta en `output`.
    """
    app.multiselect(key=f"{NEW}_outputs").set_value(outputs).run()

    assert not app.exception
    assert _dataset_variables(app, NEW) == expected


def test_choosing_a_component_shows_its_own_fields(app: AppTest) -> None:
    """Cada componente ofrece los suyos, y cambian al cambiar de componente."""
    app.pills(key=f"{NEW}_components").set_value(["mongodb", "vnet"]).run()

    assert not app.exception
    # `mongodb` tiene 5 obligatorios: se pintan sin pedir nada.
    assert _component_inputs(app, "mongodb") == {
        "mongodb_database",
        "mongodb_express_password",
        "mongodb_express_user",
        "mongodb_password",
        "mongodb_user",
    }
    # `base` ya no esta elegido, asi que sus campos desaparecen.
    assert _component_inputs(app, "base") == set()


def test_a_component_without_required_fields_asks_for_nothing(app: AppTest) -> None:
    """Elegirlo y no tocar nada es valido: se despliega con los defaults."""
    app.pills(key=f"{NEW}_components").set_value(["vnet"]).run()

    assert _component_inputs(app, "vnet") == set()
    assert app.multiselect(key=f"{NEW}_pick_vnet").options  # los opcionales estan ahi


def test_optional_fields_only_get_an_input_once_picked(app: AppTest) -> None:
    """Los 37 campos de `ueransim_both` no se pintan de golpe."""
    app.pills(key=f"{NEW}_components").set_value(["ueransim_both"]).run()
    picker = app.multiselect(key=f"{NEW}_pick_ueransim_both")

    assert len(picker.options) == 37
    assert _component_inputs(app, "ueransim_both") == set()

    chosen = picker.options[:2]
    app.multiselect(key=f"{NEW}_pick_ueransim_both").set_value(chosen).run()

    assert not app.exception
    assert len(_component_inputs(app, "ueransim_both")) == 2


def test_generating_a_descriptor_fills_the_editor(app: AppTest) -> None:
    """El camino completo del formulario, incluido que el boton sigue siendo explicito."""
    editor = app.text_area(key=NEW)
    assert editor.value == "", "el editor arranca vacio"

    app.pills(key=f"{NEW}_components").set_value(["vnet"]).run()
    app.text_input(key=f"{NEW}_name").set_value("tn-de-prueba").run()
    # Cambiar widgets no genera nada por si solo: hace falta pulsar.
    assert app.text_area(key=NEW).value == ""

    app.button(key=f"{NEW}_generate").click().run()

    assert not app.exception
    generated = app.text_area(key=NEW).value
    assert "name: tn-de-prueba" in generated
    assert "vnet:" in generated


def test_missing_required_fields_block_the_descriptor(app: AppTest) -> None:
    """`mongodb` sin sus obligatorios no llega a generarse: seria un 400."""
    app.pills(key=f"{NEW}_components").set_value(["mongodb"]).run()
    app.button(key=f"{NEW}_generate").click().run()

    assert app.text_area(key=NEW).value == ""
    assert any("mongodb" in error.value for error in app.error)


def test_the_elcm_tab_has_its_own_independent_widgets(app: AppTest) -> None:
    """Las dos pestanas comparten etiqueta y no pueden compartir estado."""
    app.multiselect(key=f"{NEW}_outputs").set_value(["csv"]).run()

    assert _dataset_variables(app, NEW) == {
        "measurement",
        "influx_host",
        "influx_port",
        "influx_bucket",
    }
    # La pestana ELCM sigue en `logs`, con sus variables vacias.
    assert app.multiselect(key=f"{ELCM}_outputs").value == ["logs"]
    assert _dataset_variables(app, ELCM) == set()


# ===== Espera en segundo plano y candado entre operaciones =====

# Los tres endpoints del ciclo de vida BLOQUEAN hasta que la fase termina (hasta
# 70 min). La UI los lanza en un hilo para no congelar la sesion: lo que se
# comprueba aqui es que el candado echa mientras dura, que la pagina sigue
# respondiendo, y que al terminar se suelta y se pinta el desenlace.

LAUNCH_BUTTONS = (f"{NEW}_launch", f"{ELCM}_launch", "teardown_launch")


RUNNING_SUMMARY = {
    "status": "Deploying the network",
    "outcome": "running",
    "message": "Activating TN tn-demo: attempt 1/3 failed (HTTP 502); retrying in 1 s",
    "steps": [{"step": "Starting up the virtual machines", "status": "running"}],
    "generated_at": "2026-08-27T00:00:00Z",
}


def _summary(self, execution_id, **kwargs):
    """Resumen en curso, con un reintento a la vista."""
    return {"execution_id": execution_id, **RUNNING_SUMMARY}


@pytest.fixture
def blocking_call(monkeypatch):
    """Sustituye la llamada de red por una que el test decide cuando termina.

    Se parchea `app.client.ApiClient`, no el modulo del script: `AppTest`
    reconstruye el modulo del script en cada pasada, asi que un parche sobre el
    no sobreviviria; `app.client` vive en `sys.modules` y si. Funciona porque se
    parchean METODOS sobre la clase, y el script hace `from app.client import
    ApiClient`: los dos nombres apuntan al mismo objeto clase.
    """
    gate = threading.Event()
    outcome: dict[str, object] = {"result": api_client.PhaseResult(200, {"execution_id": "tn-x"})}

    def _call(*args, **kwargs):
        gate.wait(timeout=30)
        if isinstance(outcome["result"], Exception):
            raise outcome["result"]
        return outcome["result"]

    for name in ("create_execution", "start_elcm", "delete_tn", "pause_tn", "resume_tn"):
        monkeypatch.setattr(api_client.ApiClient, name, _call)

    # La UI pregunta por donde va mientras espera, y al terminar deja la pestana
    # Resumen consultando esa ejecucion. Sin doble, las dos cosas saldrian a la
    # red de verdad y el fallo de conexion ensuciaria `app.error`.
    monkeypatch.setattr(api_client.ApiClient, "get_execution_summary", _summary)
    return gate, outcome


def _connected(at: AppTest) -> AppTest:
    """La UI necesita base_url y api_key para llegar a construir el cliente."""
    at.session_state["base_url"] = "http://localhost:8000"
    at.session_state["api_key"] = "clave"
    return at


def _launch_execution(at: AppTest, *, auto_start: bool = False) -> AppTest:
    """Genera un descriptor minimo y pulsa «Lanzar ejecucion».

    `auto_start` va a False salvo que se pida: con el puesto, un 200 de
    /executions NO significa «terminado» sino «la TN esta y el experimento acaba
    de empezar», que es un desenlace distinto y tiene su propia prueba.
    """
    at.pills(key=f"{NEW}_components").set_value(["vnet"]).run()
    if not auto_start:
        at.checkbox(key=f"{NEW}_auto_start").uncheck().run()
    at.button(key=f"{NEW}_generate").click().run()
    at.button(key=f"{NEW}_launch").click().run()
    return at


def test_a_running_job_locks_every_launch_button(app: AppTest, blocking_call) -> None:
    """Lanzar otra cosa mientras hay una en vuelo es lo que hace que choquen."""
    gate, _ = blocking_call
    _launch_execution(_connected(app))

    try:
        assert app.session_state["running_job"]["done"] is False
        for key in LAUNCH_BUTTONS:
            assert app.button(key=key).disabled, f"{key} deberia estar bloqueado"
    finally:
        gate.set()


def test_the_page_stays_usable_while_the_job_waits(app: AppTest, blocking_call) -> None:
    """El motivo de usar un hilo: la espera no puede congelar Estado ni Resumen.

    Si la llamada bloqueante viviera en el hilo del script, este `run()` se
    quedaria colgado hasta agotar el timeout de AppTest.
    """
    gate, _ = blocking_call
    _launch_execution(_connected(app))

    try:
        app.text_input(key="status_execution_id").set_value("tn-x").run()
        assert not app.exception
        assert app.session_state["running_job"]["done"] is False
    finally:
        gate.set()


def _finish(at: AppTest, gate: threading.Event) -> AppTest:
    """Deja terminar al hilo y repinta."""
    gate.set()
    job = at.session_state["running_job"]
    for _ in range(100):
        if job["done"]:
            break
        time.sleep(0.05)
    return at.run()


def test_the_lock_is_released_when_the_job_finishes(app: AppTest, blocking_call) -> None:
    gate, _ = blocking_call
    _launch_execution(_connected(app))
    _finish(app, gate)

    assert app.session_state["running_job"]["done"] is True
    assert not app.button(key=f"{NEW}_launch").disabled
    assert any("Completado" in message.value for message in app.success)


def test_a_partial_outcome_is_not_reported_as_success(app: AppTest, blocking_call) -> None:
    """207 significa «terminó, pero incompleto»: pintarlo en verde engaña."""
    gate, outcome = blocking_call
    outcome["result"] = api_client.PhaseResult(207, {"vpn_status": "MANUAL_REQUIRED"})
    _launch_execution(_connected(app))
    _finish(app, gate)

    assert any("incompleto" in message.value for message in app.warning)


def test_a_504_says_the_work_continues_instead_of_failing(app: AppTest, blocking_call) -> None:
    """El servidor agoto SU tope y sigue trabajando: no es un error."""
    gate, outcome = blocking_call
    outcome["result"] = api_client.ApiError("sigue en curso", status_code=504)
    _launch_execution(_connected(app))
    _finish(app, gate)

    assert not app.error
    assert any("sigue en curso" in message.value.lower() for message in app.warning)


def test_the_announcement_is_consumed_exactly_once() -> None:
    """La invariante que ninguna prueba de `AppTest` puede ver.

    Al terminar un trabajo hay que repintar la app ENTERA: el desenlace y los
    botones que el candado deshabilita viven fuera del fragment que lo vigila.
    Pero el fragment se reejecuta solo cada dos segundos, asi que el aviso tiene
    que consumirse o serian dos repintados por segundo para siempre. `AppTest` no
    dispara esos temporizadores, de ahi que se pruebe la funcion directa.
    """
    import streamlit_app

    job = streamlit_app._new_job("execution", "Despliegue")

    assert streamlit_app._take_announcement(job) is False, "sin terminar no hay nada que avisar"

    job["done"] = True
    assert streamlit_app._take_announcement(job) is True
    assert streamlit_app._take_announcement(job) is False, "el aviso no se repite"
    assert streamlit_app._take_announcement(None) is False


def test_a_failed_phase_is_reported_and_stays(app: AppTest, blocking_call) -> None:
    """La regresion de verdad: el despliegue falla y la pagina no decia nada.

    El desenlace tiene que aparecer sin que el usuario pulse nada y seguir ahi
    despues, porque la pagina se repinta sola mientras se espera.
    """
    gate, outcome = blocking_call
    outcome["result"] = api_client.ApiError(
        "Error: TNLCM new activate exhausted retries for tn_id=tn-demo (HTTP 502)",
        status_code=502,
        detail={"execution_id": "tn-demo", "status": "FAILED"},
    )
    _launch_execution(_connected(app))
    _finish(app, gate)

    assert any("exhausted retries" in message.value for message in app.error)
    assert any("HTTP 502" in message.value for message in app.error)

    app.run()
    assert any(
        "exhausted retries" in message.value for message in app.error
    ), "el error no puede borrarse en el siguiente repintado"


def test_an_unreachable_api_is_not_sold_as_work_in_progress(app: AppTest, blocking_call) -> None:
    """Con el orquestador caido no hay ninguna fase corriendo por detras.

    Los dos llegan sin codigo HTTP, pero mandar al resumen aqui seria mandar a
    buscar una ejecucion que no existe.
    """
    gate, outcome = blocking_call
    outcome["result"] = api_client.ApiError(
        "No se pudo conectar con http://localhost:8000/executions",
        reached_server=False,
    )
    _launch_execution(_connected(app))
    _finish(app, gate)

    assert any("No se pudo conectar" in message.value for message in app.error)
    assert not any("sigue en curso" in message.value for message in app.warning)


def test_a_deploy_with_auto_start_is_not_announced_as_finished(app: AppTest, blocking_call) -> None:
    """`POST /executions` responde antes de que el experimento empiece.

    Con `auto_start_elcm` el servidor lo arranca al quedar lista la TN y responde
    sin esperarlo: pintar «Completado» en verde era lo que hacia que un
    experimento fallido veinte minutos mas tarde no se lo contara a nadie.
    """
    gate, _ = blocking_call
    _launch_execution(_connected(app), auto_start=True)
    _finish(app, gate)

    assert not any("Completado" in message.value for message in app.success)
    assert any("exp-demo" in message.value for message in app.warning)
    assert app.session_state["summary_auto_refresh"] is True, "Resumen queda mirandolo"


def test_a_launch_rejected_before_starting_is_still_reported(app: AppTest) -> None:
    """Lo que se rechaza en el cliente va a la misma caja que el desenlace.

    Antes era un `st.error` dentro de la pestana, y duraba lo que durase la
    pasada: cualquier repintado -y ahora los hay solos- se lo llevaba.
    """
    _connected(app)
    app.text_area(key=NEW).set_value("infrastructure: [rota").run()
    app.button(key=f"{NEW}_launch").click().run()

    assert any("no es YAML válido" in message.value for message in app.error)
    assert app.session_state["running_job"]["done"] is True, "no se llego a lanzar nada"

    app.checkbox(key="bundle_secrets").check().run()
    assert any("no es YAML válido" in message.value for message in app.error)


def test_the_waiting_box_says_where_the_phase_is(app: AppTest, blocking_call) -> None:
    """Durante la espera solo se contaban minutos.

    Los reintentos de activate no salian de los logs del servidor; ahora llegan
    al `message` de la ejecucion y la caja de espera los ensena mientras ocurren.
    """
    gate, _ = blocking_call
    _launch_execution(_connected(app))

    try:
        assert app.session_state["running_job"]["done"] is False
        assert any("attempt 1/3 failed" in message.value for message in app.info)
    finally:
        gate.set()


def test_the_execution_id_reaches_the_other_tabs(app: AppTest, blocking_call) -> None:
    """Un `value=` no bastaba: solo se aplica la primera vez que se pinta.

    Y `st.tabs` pinta el cuerpo de todas en la primera pasada, cuando aun no hay
    ninguna ejecucion que heredar, asi que los campos se quedaban vacios y habia
    que reescribir el identificador en cada pestana.
    """
    gate, _ = blocking_call
    _launch_execution(_connected(app))

    try:
        for key in ("summary_execution_id", "elcm_execution_id", "teardown_execution_id"):
            assert app.text_input(key=key).value == "tn-demo", key
    finally:
        gate.set()


# ===== Descarga del ZIP (pestana Descargar) =====
#
# La descarga es una pestana aparte porque el caso normal es querer los
# artefactos de una TN ANTERIOR, y para eso no hace falta consultar su estado:
# el servidor los sirve leyendo `artifacts/<id>/`. Ademas el ZIP se pide en DOS
# clics —«Preparar» y luego «Descargar»— y entre uno y otro Streamlit reejecuta
# el script entero, que es donde esto estuvo roto: con la consulta y el payload
# en variables transitorias, el primer clic vaciaba la pestana y se tragaba el
# clic, asi que el fichero no llegaba a existir nunca. Lo unico que lo
# demuestra es recorrer el camino entero.

ZIP_LABEL = "Descargar tn-1.zip"


@pytest.fixture
def recorded_execution(monkeypatch):
    """Una ejecucion consultable y su ZIP, sin servidor detras.

    Devuelve lo que se le pidio al cliente: los valores de `secrets` con los que
    se llamo a la descarga —asi se comprueba que la casilla llega de verdad— y
    las consultas de estado, que en esta pestana tienen que ser cero.
    """
    calls: dict[str, list] = {"status": [], "download": []}

    def _download(self, execution_id, *, secrets=False):
        calls["download"].append(secrets)
        return b"PK\x03\x04" + b"z" * 2048

    def _status(self, execution_id):
        calls["status"].append(execution_id)
        return {"status": "COMPLETED", "tn_id": "tn-1"}

    monkeypatch.setattr(api_client.ApiClient, "get_execution", _status)
    monkeypatch.setattr(
        api_client.ApiClient,
        "get_execution_detail",
        lambda self, eid: {"tn_state": "activated"},
    )
    monkeypatch.setattr(api_client.ApiClient, "download_execution", _download)
    return calls


def _open_download(at: AppTest, execution_id: str = "tn-1") -> AppTest:
    """Deja la pestana Descargar con un execution_id escrito."""
    _connected(at)
    at.text_input(key="bundle_execution_id").set_value(execution_id).run()
    return at


def _query_status(at: AppTest, execution_id: str = "tn-1") -> AppTest:
    """Deja la pestana Estado con una ejecucion ya consultada."""
    _connected(at)
    at.text_input(key="status_execution_id").set_value(execution_id).run()
    at.button(key="status_query").click().run()
    return at


def _zip_buttons(at: AppTest) -> list:
    return [button for button in at.download_button if button.label.startswith(ZIP_LABEL)]


def test_the_zip_needs_no_status_query_at_all(app: AppTest, recorded_execution) -> None:
    """El motivo de que la descarga sea una pestana propia.

    Para los artefactos de una TN vieja basta el identificador: si esto obligara
    a consultar el estado antes, una ejecucion que el orquestador ya no tiene en
    memoria quedaria fuera de alcance.
    """
    _open_download(app)
    app.button(key="bundle_prepare").click().run()

    assert _zip_buttons(app), "con el id escrito tiene que bastar"
    assert recorded_execution["status"] == [], "no se consulta el estado para descargar"


def test_the_prepare_button_waits_for_an_execution_id(app: AppTest, recorded_execution) -> None:
    """Sin identificador no hay nada que comprimir."""
    _open_download(app, "")

    assert app.button(key="bundle_prepare").disabled
    assert recorded_execution["download"] == []


def test_preparing_the_zip_leaves_a_download_button(app: AppTest, recorded_execution) -> None:
    """La regresion de verdad: el clic en «Preparar ZIP» se perdia por el camino."""
    _open_download(app)
    app.button(key="bundle_prepare").click().run()

    assert recorded_execution["download"] == [False], "sin marcar la casilla, sin secretos"
    assert _zip_buttons(app), "el boton de descarga tiene que quedar a la vista"


def test_the_zip_stays_available_after_downloading_it(app: AppTest, recorded_execution) -> None:
    """Descargar dispara otro rerun; el payload tiene que sobrevivirlo."""
    _open_download(app)
    app.button(key="bundle_prepare").click().run()
    _zip_buttons(app)[0].click().run()

    assert _zip_buttons(app)
    assert recorded_execution["download"] == [False], "y no se vuelve a comprimir en el servidor"


def test_a_zip_prepared_with_other_parameters_is_not_offered(
    app: AppTest, recorded_execution
) -> None:
    """El fichero ya comprimido no lleva los secretos que la casilla promete."""
    _open_download(app)
    app.button(key="bundle_prepare").click().run()
    app.checkbox(key="bundle_secrets").check().run()

    assert not _zip_buttons(app), "el ZIP viejo ya no corresponde a lo que se ve"
    assert any("Vuelve a prepararlo" in message.value for message in app.info)

    app.button(key="bundle_prepare").click().run()
    assert recorded_execution["download"] == [False, True]
    assert _zip_buttons(app)
    assert any("claves privadas" in message.value for message in app.warning)


def test_a_zip_prepared_for_another_execution_is_not_offered(
    app: AppTest, recorded_execution
) -> None:
    """Encadenar descargas de varias TN no puede servir el ZIP de la anterior."""
    _open_download(app)
    app.button(key="bundle_prepare").click().run()
    app.text_input(key="bundle_execution_id").set_value("tn-vieja").run()

    assert not _zip_buttons(app)
    assert any("Vuelve a prepararlo" in message.value for message in app.info)


def test_the_status_tab_survives_a_rerun_from_another_tab(app: AppTest, recorded_execution) -> None:
    """`st.tabs` ejecuta TODAS las pestanas en cada pasada.

    Con el `st.button` transitorio que habia, tocar cualquier widget de otra
    pestana borraba la consulta de Estado por el camino.
    """
    _query_status(app)
    app.checkbox(key="bundle_secrets").check().run()

    assert app.json, "el detalle consultado no puede desaparecer"


def test_an_unexpected_client_crash_still_releases_the_lock(app: AppTest, blocking_call) -> None:
    """Sin esto, un fallo inesperado dejaria la UI bloqueada para siempre."""
    gate, outcome = blocking_call
    outcome["result"] = RuntimeError("algo raro")
    _launch_execution(_connected(app))
    _finish(app, gate)

    assert app.session_state["running_job"]["done"] is True
    assert not app.button(key=f"{NEW}_launch").disabled
    assert app.error


def test_the_handler_refuses_a_second_launch_even_if_the_button_is_forced(
    app: AppTest, blocking_call
) -> None:
    """`disabled=` solo lo respeta el navegador; el candado de verdad va dentro."""
    gate, _ = blocking_call
    _launch_execution(_connected(app))
    first = app.session_state["running_job"]

    try:
        # AppTest pulsa el boton aunque este deshabilitado, que es justo el caso
        # que hay que cubrir: el guardia del manejador.
        app.button(key=f"{NEW}_launch").click().run()
        assert app.session_state["running_job"] is first, "se lanzo un segundo trabajo"
    finally:
        gate.set()


# ===== Pestana: Conexion =====
#
# Pausar y reconectar son las dos operaciones que permiten tener varias TN
# desplegadas y elegir con cual se trabaja. Lo que se comprueba aqui es el
# cableado de la pestana: que ensena quien tiene el tunel, que manda el
# `execution_id` elegido y que hereda el candado del resto de operaciones.

EXECUTION_LIST = [
    {"execution_id": "tn-a", "status": "PAUSED", "tn_id": "tn-a", "vpn_status": "DOWN"},
    {"execution_id": "tn-b", "status": "TN_READY", "tn_id": "tn-b", "vpn_status": "UP"},
]


@pytest.fixture
def recorded_connection(monkeypatch):
    """Listado fijo y llamadas de pausa/reconexion anotadas."""
    calls: dict[str, list[str]] = {"pause": [], "resume": []}

    monkeypatch.setattr(api_client.ApiClient, "list_executions", lambda self: EXECUTION_LIST)

    def _pause(self, execution_id):
        calls["pause"].append(execution_id)
        return api_client.PhaseResult(200, {"execution_id": execution_id, "status": "PAUSED"})

    def _resume(self, execution_id):
        calls["resume"].append(execution_id)
        return api_client.PhaseResult(200, {"execution_id": execution_id, "status": "TN_READY"})

    monkeypatch.setattr(api_client.ApiClient, "pause_tn", _pause)
    monkeypatch.setattr(api_client.ApiClient, "resume_tn", _resume)
    return calls


def _open_connection(at: AppTest) -> AppTest:
    """Deja la pestana Conexion con el listado ya pedido."""
    _connected(at)
    at.button(key="connection_query").click().run()
    return at


def _await_job(at: AppTest) -> AppTest:
    """Espera a que el hilo del trabajo termine y repinta."""
    job = at.session_state["running_job"]
    for _ in range(100):
        if job["done"]:
            break
        time.sleep(0.05)
    return at.run()


def test_the_connection_tab_says_which_tn_holds_the_tunnel(
    app: AppTest, recorded_connection
) -> None:
    """Es la pregunta que se hace antes de pausar nada."""
    _open_connection(app)

    assert any("tn-b" in message.value for message in app.success)
    assert app.selectbox(key="connection_target")


def test_the_listing_is_not_requested_until_it_is_asked_for(
    app: AppTest, recorded_connection, monkeypatch
) -> None:
    """Abrir la pestana con el servidor caido no puede costar una espera."""
    asked: list[bool] = []

    def _list(self):
        asked.append(True)
        return EXECUTION_LIST

    monkeypatch.setattr(api_client.ApiClient, "list_executions", _list)
    _connected(app).run()

    assert asked == []


def test_pausing_sends_the_selected_execution(app: AppTest, recorded_connection) -> None:
    _open_connection(app)
    app.selectbox(key="connection_target").set_value("tn-b").run()
    app.button(key="connection_pause").click().run()
    _await_job(app)

    assert recorded_connection["pause"] == ["tn-b"]
    assert recorded_connection["resume"] == []


def test_resuming_sends_the_selected_execution(app: AppTest, recorded_connection) -> None:
    _open_connection(app)
    app.selectbox(key="connection_target").set_value("tn-a").run()
    app.button(key="connection_resume").click().run()
    _await_job(app)

    assert recorded_connection["resume"] == ["tn-a"]
    assert recorded_connection["pause"] == []


def test_the_tab_warns_when_another_tn_still_holds_the_tunnel(
    app: AppTest, recorded_connection
) -> None:
    """La API responde 409: avisarlo antes ahorra el viaje."""
    _open_connection(app)
    app.selectbox(key="connection_target").set_value("tn-a").run()

    assert any("tn-b" in message.value for message in app.warning)


def test_the_connection_buttons_honour_the_lock(app: AppTest, recorded_connection, blocking_call):
    """Reconectar mientras hay una fase en vuelo es justo lo que hace que choquen."""
    gate, _ = blocking_call
    _open_connection(app)
    _launch_execution(app)

    try:
        assert app.button(key="connection_pause").disabled
        assert app.button(key="connection_resume").disabled
    finally:
        gate.set()
