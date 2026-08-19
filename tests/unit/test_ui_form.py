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


@pytest.fixture
def blocking_call(monkeypatch):
    """Sustituye la llamada de red por una que el test decide cuando termina.

    Se parchea `api_client.ApiClient`, no el modulo del script: `AppTest`
    reconstruye el modulo del script en cada pasada, asi que un parche sobre el
    no sobreviviria; `api_client` vive en `sys.modules` y si.
    """
    import api_client

    gate = threading.Event()
    outcome: dict[str, object] = {"result": api_client.PhaseResult(200, {"execution_id": "tn-x"})}

    def _call(*args, **kwargs):
        gate.wait(timeout=30)
        if isinstance(outcome["result"], Exception):
            raise outcome["result"]
        return outcome["result"]

    for name in ("create_execution", "start_elcm", "delete_tn"):
        monkeypatch.setattr(api_client.ApiClient, name, _call)
    return gate, outcome


def _connected(at: AppTest) -> AppTest:
    """La UI necesita base_url y api_key para llegar a construir el cliente."""
    at.session_state["base_url"] = "http://localhost:8000"
    at.session_state["api_key"] = "clave"
    return at


def _launch_execution(at: AppTest) -> AppTest:
    """Genera un descriptor minimo y pulsa «Lanzar ejecucion»."""
    at.pills(key=f"{NEW}_components").set_value(["vnet"]).run()
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
    import api_client

    gate, outcome = blocking_call
    outcome["result"] = api_client.PhaseResult(207, {"vpn_status": "MANUAL_REQUIRED"})
    _launch_execution(_connected(app))
    _finish(app, gate)

    assert any("incompleto" in message.value for message in app.warning)


def test_a_504_says_the_work_continues_instead_of_failing(app: AppTest, blocking_call) -> None:
    """El servidor agoto SU tope y sigue trabajando: no es un error."""
    import api_client

    gate, outcome = blocking_call
    outcome["result"] = api_client.ApiError("sigue en curso", status_code=504)
    _launch_execution(_connected(app))
    _finish(app, gate)

    assert not app.error
    assert any("sigue en curso" in message.value.lower() for message in app.warning)


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
