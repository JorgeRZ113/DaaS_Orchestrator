"""UI de Streamlit para operar el DaaS Orchestrator desde el navegador.

Hace dos cosas. La primera es sustituir a Postman en el día a día: lanza
ejecuciones, consulta estado y resumen, dispara experimentos ELCM y borra la
Trial Network, hablando con la API por HTTP a través de `app.client.ApiClient`;
no accede a la lógica interna del servicio.

La segunda es la que aporta valor propio: **produce el Dataset Descriptor**. El
formulario no arma una petición, arma el fichero YAML que el anteproyecto
promete, y el usuario puede descargarlo, versionarlo en git y reenviarlo después
sin la UI delante. Por eso las pestañas de ejecución y de experimento giran
alrededor de un editor de YAML alimentado por tres fuentes intercambiables
(formulario, fichero subido y ejemplo de `examples/descriptors/`), y por eso la
UI ya no construye cuerpos JSON: la API los sigue aceptando, pero mantener tres
caminos aquí no aportaba nada. Ver `docs/UI_YAML_MIGRATION.md`.

Arranque:
    streamlit run ui/streamlit_app.py
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

import streamlit as st
import yaml

import descriptor
from app.client import (
    DATASET_MODE_VARIABLES,
    DATASET_OUTPUTS,
    DEFAULT_BASE_URL,
    ApiClient,
    ApiError,
    Descriptor,
    PhaseResult,
    variables_for_outputs,
    yaml_error_position,
)

# ===== Utilidades compartidas =====


def _get_client() -> ApiClient | None:
    """Construye el cliente a partir del panel lateral; avisa si falta config."""
    base_url = st.session_state.get("base_url", "").strip()
    api_key = st.session_state.get("api_key", "").strip()
    if not base_url or not api_key:
        st.warning("Configura Base URL y API key en el panel lateral.")
        return None
    return ApiClient(base_url=base_url, api_key=api_key)


def _show_api_error(exc: ApiError) -> None:
    """Pinta un error de API con su mensaje y, si lo hay, el detail estructurado."""
    st.error(exc.message)
    if isinstance(exc.detail, (dict, list)):
        st.json(exc.detail)


# ===== Trabajos en curso: esperar sin congelar la página =====

# Los tres endpoints del ciclo de vida BLOQUEAN: no responden hasta que la fase
# termina (la VPN resuelta, el dataset recolectado, la TN purgada). Eso es lo que
# se quiere — el desenlace viene en el código HTTP y no hay que ir a sondearlo—
# pero una llamada síncrona dentro de una pasada de script congelaría la sesión
# entera de Streamlit, y con ella las pestañas de Estado y Resumen, que es justo
# lo que interesa mirar durante una espera de 40-70 minutos.
#
# La salida es lanzar la petición en un hilo aparte. El hilo NO llama a `st.*`
# (no tiene contexto de script y no debe tocarlo): escribe en un diccionario
# normal que se crea aquí y se guarda en `session_state`, y un fragment lo lee y
# repinta. Así la petición sigue siendo una sola llamada bloqueante y la página
# sigue viva.
JOB_KEY = "running_job"

# Cada cuánto repinta el panel del trabajo en curso. Dos segundos dan sensación
# de vivo sin castigar nada: el sondeo es local, no sale a la red.
JOB_REFRESH_SECONDS = 2

# Desenlaces del código HTTP, comunes a las tres fases (`app/api/phases.py`).
_OUTCOMES = {
    200: ("success", ":material/check_circle:", "Completado."),
    207: (
        "warning",
        ":material/warning:",
        "Terminó incompleto: revisa `vpn_status` o el campo `error`.",
    ),
}


def _new_job(kind: str, label: str) -> dict[str, Any]:
    """Estado compartido entre el hilo y la página. Dict normal a propósito."""
    return {
        "kind": kind,
        "label": label,
        "started_at": time.monotonic(),
        "done": False,
        "finished_at": None,
        "result": None,
        "error": None,
        "status_code": None,
    }


def _run_job(job: dict[str, Any], call: Callable[[], PhaseResult]) -> None:
    """Cuerpo del hilo: ejecuta la llamada y deja el desenlace en `job`.

    El hilo NO toca `st.*` ni `st.session_state`: sin contexto de script, una
    escritura en `session_state` no falla, se va a un objeto global que la sesión
    real nunca lee. Aquí solo se escribe en `job`, que es un dict normal que la
    página tiene por referencia.

    `done` se marca EL ÚLTIMO, en un `finally`: es la bandera que lee la página,
    así que ponerla antes dejaría ver un resultado a medio escribir, y no
    ponerla dejaría el candado echado para siempre.
    """
    try:
        result = call()
        job["result"] = result.payload
        job["status_code"] = result.status_code
    except ApiError as exc:
        job["error"] = exc
        job["status_code"] = exc.status_code
    except BaseException as exc:  # noqa: BLE001 - el hilo no puede dejar escapar nada
        job["error"] = ApiError(f"Fallo inesperado en el cliente: {exc!r}")
    finally:
        job["finished_at"] = time.monotonic()
        job["done"] = True


def _start_job(kind: str, label: str, call: Callable[[], Any]) -> None:
    """Lanza la operación en segundo plano y deja la página utilizable."""
    job = _new_job(kind, label)
    st.session_state[JOB_KEY] = job
    thread = threading.Thread(target=_run_job, args=(job, call), daemon=True)
    thread.start()


def _current_job() -> dict[str, Any] | None:
    """El trabajo en curso o recién terminado, si lo hay."""
    return st.session_state.get(JOB_KEY)


def _job_in_flight() -> bool:
    """True mientras haya una operación esperando respuesta.

    Es el candado: mientras dure, no se puede lanzar otra ejecución, ni otro
    experimento, ni un borrado, para que no choquen entre ellos ni con las
    herramientas de abajo.
    """
    job = _current_job()
    return job is not None and not job["done"]


def _lock_help(action: str) -> str | None:
    """Explica por qué el botón está deshabilitado, si lo está."""
    job = _current_job()
    if job is None or job["done"]:
        return None
    return (
        f"Hay una operación en curso ({job['label']}). {action} podría chocar con "
        "ella, así que se espera a que termine. Mientras tanto puedes usar Estado "
        "y Resumen."
    )


def _render_job_outcome(job: dict[str, Any]) -> None:
    """Pinta el desenlace de un trabajo terminado."""
    error: ApiError | None = job["error"]

    if error is not None:
        # 504 no es un fallo: el servidor agotó SU tope y sigue trabajando.
        if error.status_code == 504:
            st.warning(
                f"{job['label']}: el servidor agotó su tope de espera, pero **la "
                "operación sigue en curso**. Míralo en la pestaña Resumen.",
                icon=":material/hourglass_top:",
            )
        else:
            st.error(f"{job['label']}: {error.message}", icon=":material/error:")
            if isinstance(error.detail, (dict, list)):
                st.json(error.detail)
        return

    kind, icon, text = _OUTCOMES.get(job["status_code"], _OUTCOMES[200])
    getattr(st, kind)(f"{job['label']}: {text}", icon=icon)
    if isinstance(job["result"], dict):
        st.json(job["result"])


@st.fragment(run_every=JOB_REFRESH_SECONDS)
def _job_watcher() -> None:
    """Vigila el trabajo en curso y repinta solo él mientras dura.

    Vive en `main()`, encima de las pestañas y en una posición FIJA, por dos
    razones. La primera es que así se ve desde cualquier pestaña: durante una
    espera larga lo normal es estar mirando Resumen, no la pestaña desde la que
    se lanzó. La segunda es que la identidad de un fragment se calcula con la
    ruta del contenedor donde se pinta, así que si delante hubiera un número
    variable de elementos cambiaría de identidad entre pasadas y el temporizador
    del navegador quedaría apuntando a uno que ya no existe.
    """
    job = _current_job()
    if job is None or job["done"]:
        return

    elapsed = time.monotonic() - job["started_at"]
    minutes, seconds = divmod(int(elapsed), 60)

    with st.container(border=True):
        st.markdown(f"⏳ **{job['label']}** — esperando respuesta del servidor")
        st.caption(
            f"Lleva {minutes} min {seconds:02d} s. La petición no responde hasta que la "
            "fase termina; mientras tanto puedes usar Estado y Resumen con normalidad."
        )

    # Al terminar hay que repintar la app ENTERA, no solo el fragment: los
    # botones que el candado deshabilita están fuera de aquí y un rerun de
    # fragment no los tocaría.
    if job["done"]:
        st.rerun(scope="app")


def _render_job_result() -> None:
    """Desenlace del último trabajo terminado, mientras no se descarte."""
    job = _current_job()
    if job is None or not job["done"]:
        return

    elapsed = (job.get("finished_at") or time.monotonic()) - job["started_at"]
    minutes, seconds = divmod(int(elapsed), 60)

    with st.container(border=True):
        _render_job_outcome(job)
        st.caption(f"Tardó {minutes} min {seconds:02d} s.")
        if st.button("Descartar", key="job_dismiss", icon=":material/close:"):
            st.session_state.pop(JOB_KEY, None)
            st.rerun()


def _job_execution_id() -> str | None:
    """`execution_id` que devolvió el último trabajo, si lo trajo."""
    job = _current_job()
    if job is None or not isinstance(job["result"], dict):
        return None
    return job["result"].get("execution_id")


# ===== Panel lateral: conexión, salud y auth TNLCM =====


def _render_health() -> None:
    """Consulta y muestra el health de servicios y de componentes."""
    base_url = st.session_state.get("base_url", "").strip()
    if not base_url:
        st.warning("Falta Base URL.")
        return
    client = ApiClient(base_url=base_url, api_key=st.session_state.get("api_key", "").strip())

    try:
        services = client.health_services()
        st.caption("Servicios (orquestador + TNLCM)")
        st.json(services)
    except ApiError as exc:
        st.error(f"services: {exc.message}")

    try:
        components = client.health_components()
        st.caption("Componentes (InfluxDB/Grafana/Prometheus/ELCM)")
        st.json(components)
    except ApiError as exc:
        st.error(f"components: {exc.message}")


def _do_login() -> None:
    """Refresca el token TNLCM con las credenciales del .env del servidor."""
    client = _get_client()
    if client is None:
        return
    try:
        result = client.login_tnlcm()
    except ApiError as exc:
        st.error(exc.message)
        return
    st.success("Login TNLCM OK.")
    st.json(result)


def _do_register(username: str, password: str, email: str, org: str) -> None:
    """Registra un usuario en TNLCM (solo necesita Base URL, no API key)."""
    base_url = st.session_state.get("base_url", "").strip()
    if not base_url:
        st.warning("Falta Base URL.")
        return
    if not username.strip() or not password.strip():
        st.error("username y password son obligatorios.")
        return
    client = ApiClient(base_url=base_url, api_key=st.session_state.get("api_key", "").strip())
    try:
        result = client.register_tnlcm(
            username.strip(),
            password.strip(),
            email.strip() or None,
            org.strip() or None,
        )
    except ApiError as exc:
        st.error(exc.message)
        return
    st.success("Usuario registrado y token guardado.")
    st.json(result)


def render_sidebar() -> None:
    """Dibuja el panel lateral: conexión, salud y autenticación TNLCM."""
    with st.sidebar:
        st.header("Conexión")
        st.session_state.setdefault("base_url", DEFAULT_BASE_URL)
        st.session_state.setdefault("api_key", "")
        st.text_input("Base URL", key="base_url")
        st.text_input("API key", key="api_key", type="password")

        st.divider()
        st.subheader("Salud")
        if st.button("Comprobar salud", width="stretch"):
            _render_health()

        st.divider()
        st.subheader("TNLCM")
        if st.button("Login (.env)", width="stretch"):
            _do_login()

        with st.expander("Registrar usuario"):
            with st.form("register_form"):
                reg_user = st.text_input("username")
                reg_pass = st.text_input("password", type="password")
                reg_email = st.text_input("email (opcional)")
                reg_org = st.text_input("org (opcional)")
                reg_submit = st.form_submit_button("Registrar")
            if reg_submit:
                _do_register(reg_user, reg_pass, reg_email, reg_org)


# ===== Pestaña: Nueva ejecución =====

# Valor de arranque del formulario. `TC_1_Preflight.yml` es el primero de la
# biblioteca (templates/ELCM/TestCase/), que es contra lo que resuelven por
# nombre de fichero `testcase_paths` y `ues_paths`.
DEFAULT_TESTCASE = "TC_1_Preflight.yml"

# Clave del editor, compartida por las tres fuentes (formulario, fichero subido y
# ejemplo). Al ser también la key del `text_area`, escribir en ella antes de
# pintarlo es lo que hace que el descriptor generado aparezca ya en el editor.
# Además hace de prefijo de todos los widgets de su pestaña: sin `st.form` el
# identificador ya no lleva el `form_id` que separaba las dos, y las etiquetas
# repetidas (`experiment.name`, `dataset.output`…) chocarían entre pestañas.
DESCRIPTOR_KEY = "descriptor_yaml"
ELCM_DESCRIPTOR_KEY = "elcm_descriptor_yaml"

# Campos que se pintan enmascarados. Es cosmético: el valor acaba en claro en el
# YAML generado, que es justamente el entregable que el usuario se descarga.
_SECRET_SUFFIXES = ("password", "token", "secret")

# Comodidades del formulario, no datos del catálogo: `admin` es el usuario que
# usan todos los ejemplos de InfluxDB.
_FIELD_DEFAULTS: dict[tuple[str, str], str] = {("base", "influxdb_user"): "admin"}


def _dataset_variables_form(prefix: str, outputs: Sequence[str]) -> dict[str, Any]:
    """Variables globales de `dataset`, solo las de los modos activos.

    Se pintan en función de `output` porque el servidor rechaza con un 422 una
    variable cuyo modo dueño no esté pedido: no ofrecerla es mejor que ofrecerla
    y avisar después. Con `output: [logs]` no queda ninguna.
    """
    names = variables_for_outputs(outputs)
    if not names:
        st.caption("Los formatos elegidos no admiten variables globales.")
        return {}

    st.caption(
        "Opcionales: si se dejan en blanco, el orquestador las deriva del "
        "despliegue y, en último término, del valor por defecto del overlay."
    )

    values: dict[str, Any] = {}
    for name in names:
        help_text = "Modos: " + ", ".join(sorted(DATASET_MODE_VARIABLES[name]))
        if name == "influx_port":
            values[name] = st.number_input(
                name,
                value=None,
                min_value=1,
                max_value=65535,
                placeholder="8086",
                help=help_text,
                key=f"{prefix}_var_{name}",
            )
        else:
            values[name] = st.text_input(name, help=help_text, key=f"{prefix}_var_{name}")
    return values


def _is_secret(name: str) -> bool:
    """Un campo se enmascara si su nombre acaba en algo que suena a credencial."""
    return name.endswith(_SECRET_SUFFIXES)


def _field_input(prefix: str, field: descriptor.ComponentField) -> str:
    """Un input por campo editable del componente.

    La sección entra en la key porque hay nombres repetidos dentro del mismo
    componente (`int_p4_sw` tiene `name` en `network` y en `vm`), y dos widgets
    con la misma key son un error aunque estén en expanders distintos.
    """
    return st.text_input(
        f"{field.label} *" if field.required else field.label,
        value=_FIELD_DEFAULTS.get((field.component, field.name), ""),
        type="password" if _is_secret(field.name) else "default",
        help=f"Sección `{field.section}` del overlay." if field.ambiguous else None,
        key=f"{prefix}_field_{field.component}_{field.section}_{field.name}",
    )


def _component_editor(prefix: str, selected: Sequence[str]) -> dict[descriptor.ComponentField, str]:
    """Campos de cada componente elegido: obligatorios siempre, opcionales a la carta.

    Los opcionales pasan por un selector previo en vez de pintarse todos:
    `ueransim_split` tiene 39 campos y llenar la página de cajas vacías no ayuda.
    Los obligatorios no pueden esconderse ahí — nombrar el componente sin ellos
    es un 400, no «usa los defaults».
    """
    values: dict[descriptor.ComponentField, str] = {}

    for position, component in enumerate(selected):
        fields = descriptor.component_fields(component)
        required = [field for field in fields if field.required]
        optional = [field for field in fields if not field.required]

        with st.expander(f"{component} — {len(fields)} campos editables", expanded=position == 0):
            if required:
                st.caption("Obligatorios (marcados con \\*): sin ellos el descriptor no vale.")
                for field in required:
                    values[field] = _field_input(prefix, field)
            else:
                st.caption(
                    "Ningún campo obligatorio: puedes dejarlo tal cual y se despliega "
                    "con los valores por defecto de su overlay."
                )

            chosen = st.multiselect(
                "Campos a personalizar",
                options=optional,
                format_func=lambda field: field.label,
                help="Lo que no toques se queda con el valor por defecto del overlay.",
                key=f"{prefix}_pick_{component}",
            )
            for field in chosen:
                values[field] = _field_input(prefix, field)

    return values


def _descriptor_from_form() -> str | None:
    """Formulario que genera el descriptor; devuelve su YAML o None si no se pulsó.

    Ya no vive en un `st.form`: dentro de uno ningún widget dispara rerun hasta
    enviar, y entonces ni las variables de `dataset` podrían reaccionar a
    `output` ni los campos al componente elegido. El botón sigue siendo
    explícito, así que tocar el formulario no pisa lo editado a mano en el YAML.
    """
    prefix = DESCRIPTOR_KEY

    name = st.text_input("Nombre de la TN", value="tn-demo", key=f"{prefix}_name")
    col1, col2 = st.columns(2)
    with col1:
        lib_type = st.text_input("library_reference_type", value="branch", key=f"{prefix}_lib_type")
    with col2:
        lib_value = st.text_input(
            "library_reference_value", value="develop", key=f"{prefix}_lib_value"
        )

    st.markdown("**Componentes a desplegar**")
    selected = st.pills(
        "Componentes",
        options=descriptor.list_components(),
        selection_mode="multi",
        default=["base"],
        label_visibility="collapsed",
        key=f"{prefix}_components",
        help=(
            "`base` despliega el núcleo común (InfluxDB + Grafana) que el resto de "
            "componentes da por supuesto."
        ),
    )
    values = _component_editor(prefix, selected or [])

    st.markdown("**Dataset**")
    outputs = st.multiselect(
        "dataset.output",
        options=list(DATASET_OUTPUTS),
        default=["logs"],
        key=f"{prefix}_outputs",
    )
    with st.expander("Variables globales de dataset"):
        variables = _dataset_variables_form(prefix, outputs)

    col3, col4 = st.columns(2)
    with col3:
        auto_start = st.checkbox("auto_start_elcm", value=True, key=f"{prefix}_auto_start")
    with col4:
        ephemeral = st.checkbox("ephemeral_tn", value=False, key=f"{prefix}_ephemeral")

    st.markdown("**Experimento inicial** (obligatorio si auto_start_elcm)")
    exp_name = st.text_input("experiment.name", value="exp-demo", key=f"{prefix}_exp_name")
    testcases = st.text_area(
        "testcase_paths (uno por línea)", value=DEFAULT_TESTCASE, key=f"{prefix}_testcases"
    )
    ues = st.text_area("ues_paths (uno por línea)", value="", key=f"{prefix}_ues")

    if not st.button("Generar descriptor", icon=":material/description:", key=f"{prefix}_generate"):
        return None

    if not name.strip():
        st.error("El nombre de la TN es obligatorio.")
        return None
    if not outputs:
        st.error("Selecciona al menos un formato en dataset.output.")
        return None

    missing = descriptor.missing_required(selected or [], values)
    if missing:
        for component, fields in missing.items():
            st.error(f"El componente '{component}' requiere: {', '.join(fields)}.")
        return None

    parameters = {
        key: value.strip()
        for key, value in (
            ("library_reference_type", lib_type),
            ("library_reference_value", lib_value),
        )
        if value.strip()
    }

    experiment = None
    if exp_name.strip():
        experiment = descriptor.build_experiment(exp_name, testcases, ues)
    elif auto_start:
        st.error("Con auto_start_elcm=True debes indicar experiment.name.")
        return None

    return descriptor.to_yaml(
        descriptor.build_descriptor(
            name=name,
            component=descriptor.build_component(selected or [], values),
            parameters=parameters,
            experiment=experiment,
            dataset=descriptor.build_dataset(outputs, variables),
            auto_start_elcm=auto_start,
            ephemeral_tn=ephemeral,
        )
    )


def _descriptor_from_upload(key: str) -> str | None:
    """Sube un `.yaml`/`.yml` y devuelve su texto, o None si no hay fichero."""
    uploaded = st.file_uploader("Descriptor en YAML", type=["yaml", "yml"], key=f"{key}_upload")
    if uploaded is None:
        return None
    try:
        return uploaded.getvalue().decode("utf-8")
    except UnicodeDecodeError:
        st.error("El fichero no está codificado en UTF-8.")
        return None


def _descriptor_from_example(key: str, only: str | None = None) -> str | None:
    """Carga uno de los descriptores comentados de `examples/descriptors/`."""
    names = descriptor.list_examples()
    if only is not None:
        names = [name for name in names if name == only]
    if not names:
        st.caption("No hay ejemplos disponibles (la UI no está junto al repositorio).")
        return None

    choice = st.selectbox("Ejemplo", options=names, key=f"{key}_example")
    if not st.button("Cargar ejemplo", key=f"{key}_load", icon=":material/file_open:"):
        return None
    return descriptor.read_example(choice)


def _fill_editor(key: str, text: str | None) -> None:
    """Vuelca un descriptor en el editor.

    Escribir en `session_state` solo es legal ANTES de que el `text_area` de esa
    misma key se pinte en este mismo rerun; de ahí el orden de `_render_editor`.
    """
    if text is not None:
        st.session_state[key] = text


def _render_editor(
    key: str, *, filename: str, label: str = "Descriptor (YAML)", height: int = 420
) -> str:
    """Editor del descriptor, con descarga y comprobación de sintaxis en cliente."""
    st.text_area(
        label,
        key=key,
        height=height,
        help="Es el fichero que se envía, y el que puedes descargar y versionar.",
    )
    text = st.session_state.get(key, "")

    col1, col2 = st.columns([1, 3])
    with col1:
        st.download_button(
            "Descargar",
            data=text,
            file_name=filename,
            mime="application/yaml",
            icon=":material/download:",
            disabled=not text.strip(),
            width="stretch",
        )
    with col2:
        if text.strip():
            try:
                descriptor.parse_yaml(text)
            except (yaml.YAMLError, ValueError) as exc:
                # `problem` es la frase corta del error; `str(exc)` repite además
                # la posición y el fragmento, que aquí ya se pintan aparte.
                problem = getattr(exc, "problem", None) or str(exc)
                mark = getattr(exc, "problem_mark", None)
                where = f" (línea {mark.line + 1}, columna {mark.column + 1})" if mark else ""
                st.error(f"YAML inválido{where}: {problem}", icon=":material/error:")
            else:
                st.success("Sintaxis YAML correcta.", icon=":material/check:")
    return text


def _show_yaml_error_position(exc: ApiError, text: str) -> None:
    """Señala en el propio descriptor la línea que el servidor marca como rota.

    Es la ventaja de UX que trae el camino YAML: el 400 lleva línea y columna, y
    el JSON nunca las dio.
    """
    position = yaml_error_position(exc.detail)
    if position is None:
        return
    line, column = position
    lines = text.splitlines()
    start = max(0, line - 3)
    excerpt = "\n".join(
        f"{number:>4} {'>' if number == line else ' '} {content}"
        for number, content in enumerate(lines[start : line + 2], start=start + 1)
    )
    st.code(excerpt, language=None)
    st.caption(f"El servidor sitúa el error en la línea {line}, columna {column}.")


def _descriptor_sources(key: str, *, only_example: str | None = None) -> None:
    """Selector de origen del descriptor; deja el resultado en el editor."""
    source = st.segmented_control(
        "De dónde sale el descriptor",
        options=["Formulario", "Fichero", "Ejemplo"],
        default="Formulario",
        key=f"{key}_source",
    )
    if source == "Fichero":
        _fill_editor(key, _descriptor_from_upload(key))
    elif source == "Ejemplo":
        _fill_editor(key, _descriptor_from_example(key, only=only_example))
    elif source == "Formulario":
        if key == DESCRIPTOR_KEY:
            _fill_editor(key, _descriptor_from_form())
        else:
            _fill_editor(key, _elcm_request_from_form())


def tab_new_execution() -> None:
    """Genera, edita y envía el Dataset Descriptor de una nueva ejecución."""
    st.subheader("Nueva ejecución")
    st.caption(
        "El descriptor es el entregable: sale del formulario, se puede descargar, "
        "versionar en git y reenviar después sin la UI, desde consola o CI."
    )

    st.session_state.setdefault(DESCRIPTOR_KEY, "")
    _descriptor_sources(DESCRIPTOR_KEY)

    st.divider()
    text = _render_editor(DESCRIPTOR_KEY, filename="descriptor.yaml")

    launched = st.button(
        "Lanzar ejecución",
        icon=":material/rocket_launch:",
        type="primary",
        key=f"{DESCRIPTOR_KEY}_launch",
        disabled=_job_in_flight(),
        help=_lock_help("Lanzar otra ejecución"),
    )
    if not launched:
        return
    # `disabled=` no lo impone el servidor de Streamlit, solo el navegador: el
    # candado de verdad se comprueba aqui.
    if _job_in_flight():
        return

    client = _get_client()
    if client is None:
        return
    if not text.strip():
        st.warning("El descriptor está vacío: genéralo, súbelo o carga un ejemplo.")
        return

    # La sintaxis se comprueba antes de lanzar el hilo: un YAML roto se responde
    # con un 400 inmediato y no merece bloquear la UI ni ocupar el candado.
    try:
        descriptor.parse_yaml(text)
    except (yaml.YAMLError, ValueError) as exc:
        st.error(f"El descriptor no es YAML válido: {exc}", icon=":material/error:")
        return

    # El nombre de la TN se guarda ya: el ZIP y el resumen se consultan por él, y
    # con una espera de 40 minutos por delante conviene tenerlo a mano desde ya.
    parsed = descriptor.parse_yaml(text)
    name = (parsed.get("infrastructure") or {}).get("name")
    if isinstance(name, str) and name.strip():
        st.session_state["last_execution_id"] = name.strip()

    payload = Descriptor(filename="descriptor.yaml", content=text.encode("utf-8"))
    _start_job(
        "execution", "Despliegue de la Trial Network", lambda: client.create_execution(payload)
    )
    st.rerun()


# ===== Pestaña: Estado =====


def tab_status() -> None:
    """Consulta estado resumido + detalle de una ejecución."""
    st.subheader("Estado de una ejecución")
    execution_id = st.text_input(
        "execution_id",
        value=st.session_state.get("last_execution_id", ""),
        key="status_execution_id",
    )
    # La consulta se recuerda en vez de vivir un solo rerun. Debajo hay una
    # casilla y dos botones —los del ZIP—, y con un `st.button` transitorio el
    # primer clic en cualquiera de ellos vuelve a dar False aquí: la pestaña se
    # vaciaba entera y el clic se perdía por el camino. Mismo patrón que Resumen.
    if st.button("Consultar", key="status_query"):
        st.session_state["status_requested"] = True
    if not st.session_state.get("status_requested"):
        return

    client = _get_client()
    if client is None:
        return
    if not execution_id.strip():
        st.warning("Introduce un execution_id.")
        return

    try:
        status = client.get_execution(execution_id.strip())
        detail = client.get_execution_detail(execution_id.strip())
    except ApiError as exc:
        _show_api_error(exc)
        return

    cols = st.columns(3)
    cols[0].metric("Estado", str(status.get("status", "?")))
    cols[1].metric("tn_id", str(status.get("tn_id") or "—"))
    # `tn_state` es lo que TNLCM dice AHORA de la TN (created/activated/destroyed),
    # no lo que guardó el orquestador: por eso vale la pena enfrentarlo al estado
    # propio, que es donde se ven las desincronizaciones. Queda a null si la
    # ejecución todavía no tiene TN o si no se pudo consultar.
    cols[2].metric("tn_state (TNLCM)", str(detail.get("tn_state") or "—"))

    if status.get("message"):
        st.info(status["message"])
    if detail.get("error"):
        st.error(detail["error"], icon=":material/error:")

    st.markdown("**Detalle completo**")
    st.json(detail)


# ===== Pestaña: Conexión =====


def _connection_rows(executions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Las columnas que importan para decidir a qué TN conectarse."""
    return [
        {
            "execution_id": item.get("execution_id", "?"),
            "status": item.get("status", "?"),
            "tn_id": item.get("tn_id") or "—",
            "vpn_status": item.get("vpn_status") or "—",
        }
        for item in executions
    ]


def tab_connection() -> None:
    """Pausa una Trial Network o vuelve a conectarse a ella, sin borrarla."""
    st.subheader("Conexión con la Trial Network")
    st.caption(
        "Pausar baja el túnel WireGuard y deja la TN viva en TNLCM: es la forma de "
        "apartarla para trabajar con otra. Reconectar la devuelve a TN_READY sin "
        "redesplegar y **sin tocar su descriptor ni sus experimentos**. Solo puede "
        "haber un túnel levantado a la vez."
    )

    # Misma pauta que Estado: no se llama a la API hasta que se pide, para que
    # abrir la pestaña con el servidor caído no cueste una espera.
    if st.button("Ver ejecuciones", key="connection_query", icon=":material/refresh:"):
        st.session_state["connection_requested"] = True
    if not st.session_state.get("connection_requested"):
        return

    client = _get_client()
    if client is None:
        return

    try:
        executions = client.list_executions()
    except ApiError as exc:
        _show_api_error(exc)
        return

    if not executions:
        st.info("Todavía no hay ninguna ejecución registrada.")
        return

    connected = next(
        (item["execution_id"] for item in executions if item.get("vpn_status") == "UP"), None
    )
    if connected:
        st.success(f"Túnel levantado ahora mismo: **{connected}**", icon=":material/vpn_lock:")
    else:
        st.info("Ninguna TN tiene el túnel levantado.", icon=":material/vpn_key_off:")

    st.dataframe(_connection_rows(executions), hide_index=True, width="stretch")

    options = [item.get("execution_id", "?") for item in executions]
    last = st.session_state.get("last_execution_id", "")
    target = st.selectbox(
        "execution_id",
        options,
        index=options.index(last) if last in options else 0,
        key="connection_target",
    )

    blocked_by_other = connected is not None and connected != target
    if blocked_by_other:
        st.warning(
            f"«{connected}» sigue conectada. Pausa esa primero: la API rechaza "
            "reconectar mientras haya otro túnel arriba.",
            icon=":material/warning:",
        )

    cols = st.columns(2)
    paused = cols[0].button(
        "Pausar",
        disabled=_job_in_flight(),
        key="connection_pause",
        icon=":material/pause_circle:",
        help=_lock_help("Pausar la TN"),
    )
    resumed = cols[1].button(
        "Conectar",
        disabled=_job_in_flight(),
        key="connection_resume",
        icon=":material/play_circle:",
        help=_lock_help("Conectar la TN"),
    )

    if not paused and not resumed:
        return
    if _job_in_flight():
        return

    if paused:
        _start_job("pause", f"Pausa de la TN de {target}", lambda: client.pause_tn(target))
    else:
        _start_job("resume", f"Conexión con la TN de {target}", lambda: client.resume_tn(target))
    st.rerun()


# ===== Pestaña: Descargar =====

BUNDLE_KEY = "prepared_bundle"


def tab_download() -> None:
    """Recupera el ZIP de una ejecución cualquiera, sin pasar por su estado.

    Vive fuera de Estado a propósito: para bajarse los artefactos de una TN
    anterior no hace falta consultar nada, basta con su identificador. El
    servidor los sirve leyendo `artifacts/<execution_id>/`, así que siguen
    disponibles aunque la TN ya esté borrada —el teardown no toca esa carpeta—
    y aunque el orquestador se haya reiniciado desde entonces.
    """
    st.subheader("Descargar los artefactos de una ejecución")
    execution_id = st.text_input(
        "execution_id",
        value=st.session_state.get("last_execution_id", ""),
        key="bundle_execution_id",
        help=(
            "El nombre de la TN (`infrastructure.name`) con el que se lanzó. "
            "Vale el de cualquier ejecución pasada, no solo la de esta sesión."
        ),
    )
    st.caption(
        "El ZIP lleva el descriptor que la produjo, el resumen, los datasets "
        "recolectados y los artefactos intermedios. Si el orquestador todavía "
        "conoce la ejecución, añade un README con sus metadatos."
    )
    _render_bundle_download(execution_id.strip())


def _render_bundle_download(execution_id: str) -> None:
    """Descarga el ZIP con todo lo que ha dejado la ejecución.

    La descarga se pide solo al pulsar, no al pintar la pestaña: el servidor
    comprime la carpeta entera y no tiene sentido hacerlo en cada refresco.

    El ZIP preparado se guarda en `session_state` porque pulsar «Descargar»
    dispara otro rerun, y un payload en una variable local desaparecería justo
    cuando el usuario va a usarlo. Se guarda junto a los parámetros con los que
    se pidió: si cambian, el fichero deja de ser el que describe la pantalla.
    """
    secrets = st.checkbox(
        "Incluir ficheros con claves de acceso",
        key="bundle_secrets",
        help=(
            "Por defecto el ZIP deja fuera la configuración de WireGuard y los "
            "informes crudos de TNLCM: llevan la clave privada del túnel y el "
            "token de InfluxDB. Márcalo solo si los necesitas, y trata el "
            "fichero en consecuencia."
        ),
    )
    prepare = st.button(
        "Preparar ZIP",
        key="bundle_prepare",
        icon=":material/folder_zip:",
        disabled=not execution_id,
        help=None if execution_id else "Escribe antes el execution_id.",
    )
    if prepare:
        # `disabled=` no lo impone el servidor de Streamlit, solo el navegador.
        if not execution_id:
            return
        client = _get_client()
        if client is None:
            return
        try:
            payload = client.download_execution(execution_id, secrets=secrets)
        except ApiError as exc:
            _show_api_error(exc)
            return
        st.session_state[BUNDLE_KEY] = {
            "execution_id": execution_id,
            "secrets": secrets,
            "payload": payload,
        }

    bundle = st.session_state.get(BUNDLE_KEY)
    if bundle is None:
        return
    # Un ZIP preparado para otra ejecución, o con la casilla en la otra
    # posición, ya no es el que la pantalla describe: antes que entregar un
    # fichero que no corresponde, se pide prepararlo de nuevo.
    if bundle["execution_id"] != execution_id or bundle["secrets"] != secrets:
        st.info(
            "El ZIP preparado era para otros parámetros. Vuelve a prepararlo.",
            icon=":material/info:",
        )
        return

    payload = bundle["payload"]
    st.download_button(
        f"Descargar {execution_id}.zip ({len(payload) / 1024:.0f} KiB)",
        data=payload,
        file_name=f"{execution_id}.zip",
        mime="application/zip",
        icon=":material/download:",
        key="bundle_download",
    )
    if secrets:
        st.warning(
            "Este ZIP contiene claves privadas y tokens en claro.",
            icon=":material/key:",
        )


# ===== Pestaña: Resumen =====

# Un despliegue TNLCM tarda ~4-5 min, asi que sondear cada 5 s da sensacion de
# progreso sin castigar a la API.
SUMMARY_REFRESH_SECONDS = 5

# El backend devuelve el resumen en ingles a proposito (la telemetria es
# internacional). Aqui solo se traduce el estado de cada paso, que es un enum
# corto y cerrado; las etiquetas de los pasos se pintan tal cual para no
# duplicar el catalogo de `app/observability/execution_summary.py`.
_STEP_STATUS = {
    "ok": "Completado",
    "error": "Error",
    "running": "En curso",
    "pending": "Pendiente",
    "skipped": "Omitido",
}


def _render_steps(steps: list[dict[str, Any]]) -> None:
    """Pinta una lista de pasos como tabla (paso, duración, estado)."""
    if not steps:
        st.caption("Todavía no hay pasos registrados.")
        return

    # `detail` solo viene en los pasos que fallan: si no hay ninguno, se omite
    # la columna en vez de mostrarla vacía.
    show_detail = any(step.get("detail") for step in steps)

    rows: list[dict[str, Any]] = []
    for step in steps:
        row: dict[str, Any] = {
            "Paso": step.get("step", "?"),
            "Duración": step.get("duration") or "—",
            "Estado": _STEP_STATUS.get(step.get("status", ""), step.get("status", "?")),
            "Intentos": step.get("attempts"),
        }
        if show_detail:
            row["Detalle"] = step.get("detail")
        rows.append(row)

    st.dataframe(
        rows,
        hide_index=True,
        column_config={
            "Paso": st.column_config.TextColumn(width="large"),
            "Intentos": st.column_config.NumberColumn(help="Solo si el paso se reintentó"),
        },
    )


def _render_summary(summary: dict[str, Any]) -> None:
    """Pinta el resumen completo: KPIs, aviso de estado, pasos y resultados."""
    with st.container(horizontal=True):
        st.metric("Estado", summary.get("status") or "?", border=True)
        st.metric("Duración total", summary.get("total_duration") or "—", border=True)
        st.metric("Red", summary.get("network") or "—", border=True)
        if summary.get("experiments_total"):
            st.metric(
                "Experimentos correctos",
                f"{summary.get('experiments_successful', 0)} / {summary['experiments_total']}",
                border=True,
            )

    if summary.get("what_went_wrong"):
        st.error(summary["what_went_wrong"], icon=":material/error:")
    elif summary.get("outcome") == "ok":
        st.success(
            summary.get("message") or "Ejecución completada.",
            icon=":material/check_circle:",
        )
    elif summary.get("message"):
        st.info(summary["message"], icon=":material/hourglass_top:")

    _render_steps(summary.get("steps") or [])

    dashboards = summary.get("dashboards") or []
    results = summary.get("results") or []
    if dashboards or results:
        with st.container(border=True):
            st.markdown("**Resultados**")
            for url in dashboards:
                st.link_button("Abrir dashboard de Grafana", url, icon=":material/monitoring:")
            for path in results:
                st.code(path, language=None)

    with st.expander("Detalle técnico"):
        st.caption("Pasos internos del orquestador; normalmente no hace falta mirarlos.")
        _render_steps(summary.get("technical_steps") or [])


def _render_summary_panel() -> None:
    """Descarga el resumen del execution_id del formulario y lo pinta."""
    execution_id = st.session_state.get("summary_execution_id", "").strip()
    if not execution_id:
        st.warning("Introduce un execution_id.")
        return

    client = _get_client()
    if client is None:
        return

    try:
        summary = client.get_execution_summary(execution_id)
    except ApiError as exc:
        _show_api_error(exc)
        return

    _render_summary(summary)
    st.caption(f"Actualizado: {summary.get('generated_at', '?')}")


@st.fragment(run_every=SUMMARY_REFRESH_SECONDS)
def _summary_live_panel() -> None:
    """Mismo panel, refrescándose solo él cada N segundos.

    Al vivir en un fragment, el sondeo no vuelve a ejecutar el resto de la app
    ni pierde lo que haya escrito el usuario en las otras pestañas.
    """
    _render_summary_panel()


def tab_summary() -> None:
    """Resumen legible de una ejecución (GET /executions/{id}/summary)."""
    st.subheader("Resumen de la ejecución")
    st.caption(
        "Qué ha pasado en cada fase, cuánto ha tardado y dónde han quedado los "
        "resultados. Se construye en vivo, así que puedes mirarlo mientras la "
        "Trial Network se está desplegando."
    )

    execution_id = st.text_input(
        "execution_id",
        value=st.session_state.get("last_execution_id", ""),
        key="summary_execution_id",
    )

    auto = st.toggle(
        f"Actualizar cada {SUMMARY_REFRESH_SECONDS} s",
        key="summary_auto_refresh",
        help="Útil mientras la ejecución está en curso: un despliegue tarda ~4-5 min.",
    )
    if st.button("Consultar", icon=":material/refresh:"):
        st.session_state["summary_requested"] = True

    if not st.session_state.get("summary_requested"):
        return

    if auto:
        _summary_live_panel()
    else:
        _render_summary_panel()

    with st.expander("Informe en Markdown (el mismo summary.md de artifacts/)"):
        # La descarga se hace solo bajo demanda: el contenido de un expander se
        # evalua aunque este plegado.
        if st.button("Generar informe", icon=":material/description:", key="summary_md"):
            client = _get_client()
            if client is not None and execution_id.strip():
                try:
                    report = client.get_execution_summary(execution_id.strip(), as_markdown=True)
                except ApiError as exc:
                    _show_api_error(exc)
                else:
                    st.download_button(
                        "Descargar summary.md",
                        data=report,
                        file_name=f"summary_{execution_id.strip()}.md",
                        mime="text/markdown",
                        icon=":material/download:",
                    )
                    st.markdown(report)


# ===== Pestaña: Experimento ELCM =====


def _elcm_request_from_form() -> str | None:
    """Formulario del cuerpo de /elcm; devuelve su YAML o None si no se envió.

    El cuerpo NO es un Dataset Descriptor: lleva solo `experiment` y `dataset`,
    porque la infraestructura ya existe y no se vuelve a describir.
    """
    prefix = ELCM_DESCRIPTOR_KEY

    exp_name = st.text_input("experiment.name", value="exp-demo", key=f"{prefix}_exp_name")
    testcases = st.text_area(
        "testcase_paths (uno por línea)", value=DEFAULT_TESTCASE, key=f"{prefix}_testcases"
    )
    ues = st.text_area("ues_paths (uno por línea)", value="", key=f"{prefix}_ues")
    outputs = st.multiselect(
        "dataset.output",
        options=list(DATASET_OUTPUTS),
        default=["logs"],
        key=f"{prefix}_outputs",
    )
    with st.expander("Variables globales de dataset"):
        variables = _dataset_variables_form(prefix, outputs)

    if not st.button("Generar cuerpo", icon=":material/description:", key=f"{prefix}_generate"):
        return None

    if not exp_name.strip():
        st.error("experiment.name es obligatorio.")
        return None
    if not outputs:
        st.error("Selecciona al menos un formato en dataset.output.")
        return None

    return descriptor.to_yaml(
        descriptor.build_elcm_request(
            experiment=descriptor.build_experiment(exp_name, testcases, ues),
            dataset=descriptor.build_dataset(outputs, variables),
        )
    )


def tab_elcm() -> None:
    """Lanza un experimento ELCM sobre una TN viva."""
    st.subheader("Lanzar experimento ELCM")
    st.caption(
        "Se puede llamar tantas veces como experimentos se quieran encadenar sobre "
        "la misma TN. Cada nombre debe ser único dentro de la TN: ELCM los registra "
        "por nombre y un duplicado se rechaza con 409."
    )

    execution_id = st.text_input(
        "execution_id",
        value=st.session_state.get("last_execution_id", ""),
        key="elcm_execution_id",
    )

    st.session_state.setdefault(ELCM_DESCRIPTOR_KEY, "")
    _descriptor_sources(ELCM_DESCRIPTOR_KEY, only_example=descriptor.ELCM_EXAMPLE)

    st.divider()
    text = _render_editor(
        ELCM_DESCRIPTOR_KEY,
        filename="experimento.yaml",
        label="Cuerpo del experimento (YAML)",
        height=300,
    )

    launched = st.button(
        "Lanzar experimento",
        icon=":material/play_arrow:",
        type="primary",
        key=f"{ELCM_DESCRIPTOR_KEY}_launch",
        disabled=_job_in_flight(),
        help=_lock_help("Lanzar otro experimento"),
    )
    if not launched:
        return
    if _job_in_flight():
        return

    client = _get_client()
    if client is None:
        return
    if not execution_id.strip():
        st.error("execution_id es obligatorio.")
        return
    if not text.strip():
        st.warning("El cuerpo está vacío: genéralo, súbelo o carga el ejemplo.")
        return

    try:
        descriptor.parse_yaml(text)
    except (yaml.YAMLError, ValueError) as exc:
        st.error(f"El cuerpo no es YAML válido: {exc}", icon=":material/error:")
        return

    payload = Descriptor(filename="experimento.yaml", content=text.encode("utf-8"))
    target = execution_id.strip()
    _start_job(
        "elcm",
        f"Experimento sobre {target}",
        lambda: client.start_elcm(target, payload),
    )
    st.rerun()


# ===== Pestaña: Borrar TN =====


def tab_teardown() -> None:
    """Dispara el borrado de la Trial Network de una ejecución."""
    st.subheader("Borrar Trial Network")
    execution_id = st.text_input(
        "execution_id",
        value=st.session_state.get("last_execution_id", ""),
        key="teardown_execution_id",
    )
    confirm = st.checkbox("Confirmo el borrado de la TN", key="teardown_confirm")
    launched = st.button(
        "Borrar TN",
        disabled=not confirm or _job_in_flight(),
        key="teardown_launch",
        help=_lock_help("Borrar la TN"),
    )
    if not launched:
        return
    if _job_in_flight():
        return

    client = _get_client()
    if client is None:
        return
    if not execution_id.strip():
        st.warning("Introduce un execution_id.")
        return

    target = execution_id.strip()
    _start_job("teardown", f"Borrado de la TN de {target}", lambda: client.delete_tn(target))
    st.rerun()


# ===== Entrada principal =====


def main() -> None:
    """Configura la página, el panel lateral y las pestañas de operación."""
    st.set_page_config(page_title="DaaS Orchestrator UI", page_icon="🛰️", layout="wide")
    st.title("DaaS Orchestrator")
    st.caption("UI para operar el orquestador sin Postman")

    # Los contenedores se reservan ANTES de rellenarlos y en este orden a
    # proposito: la identidad de un fragment depende de la ruta del contenedor
    # donde se pinta, asi que el vigilante tiene que ir en un sitio fijo y el
    # bloque de resultado —cuyo numero de elementos varia— por detras.
    watch_box = st.container()
    result_box = st.container()
    with watch_box:
        _job_watcher()
    with result_box:
        _render_job_result()

    render_sidebar()

    # El orden sigue el ciclo real: desplegar, mirar cómo va, comprobar, elegir a
    # qué TN se está conectado, hacer el experimento, llevarse los datos y, solo
    # entonces, borrar la TN.
    tabs = st.tabs(
        [
            "Nueva ejecución",
            "Resumen",
            "Estado",
            "Conexión",
            "Experimento ELCM",
            "Descargar",
            "Borrar TN",
        ]
    )
    with tabs[0]:
        tab_new_execution()
    with tabs[1]:
        tab_summary()
    with tabs[2]:
        tab_status()
    with tabs[3]:
        tab_connection()
    with tabs[4]:
        tab_elcm()
    with tabs[5]:
        tab_download()
    with tabs[6]:
        tab_teardown()


if __name__ == "__main__":
    main()
