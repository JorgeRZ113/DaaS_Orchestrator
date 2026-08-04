"""UI de Streamlit para operar el DaaS Orchestrator desde el navegador.

Sustituye a Postman para el día a día: rellena formularios, lanza ejecuciones,
consulta estado, dispara experimentos ELCM y borra la Trial Network. Habla con
la API existente (FastAPI) a través de `api_client.ApiClient`; no accede a la
lógica interna del servicio.

Arranque:
    streamlit run ui/streamlit_app.py
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from api_client import DATASET_OUTPUTS, ApiClient, ApiError

DEFAULT_BASE_URL = "http://localhost:8000"


# ===== Utilidades compartidas =====


def _lines_to_list(text: str) -> list[str]:
    """Convierte un textarea (una entrada por línea) en lista sin vacíos."""
    return [line.strip() for line in text.splitlines() if line.strip()]


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


def _build_execution_body(
    *,
    name: str,
    lib_type: str,
    lib_value: str,
    influx_user: str,
    influx_password: str,
    grafana_password: str,
    outputs: list[str],
    auto_start: bool,
    ephemeral: bool,
    exp_name: str,
    testcases: str,
    ues: str,
    component_json: str,
) -> dict[str, Any] | None:
    """Construye el body de POST /executions; devuelve None si hay error de forma.

    Solo se incluyen campos con valor real: los vacíos se omiten para no chocar
    con el rechazo de strings vacíos del backend.
    """
    if not name.strip():
        st.error("El nombre de la TN es obligatorio.")
        return None
    if not outputs:
        st.error("Selecciona al menos un formato en dataset.output.")
        return None

    infrastructure: dict[str, Any] = {"name": name.strip()}

    parameters: dict[str, Any] = {}
    if lib_type.strip():
        parameters["library_reference_type"] = lib_type.strip()
    if lib_value.strip():
        parameters["library_reference_value"] = lib_value.strip()
    if parameters:
        infrastructure["parameters"] = parameters

    if component_json.strip():
        try:
            infrastructure["component"] = json.loads(component_json)
        except json.JSONDecodeError as exc:
            st.error(f"JSON de component inválido: {exc}")
            return None
    else:
        # El componente 'base' es todo-o-nada: el backend exige sus tres campos
        # obligatorios (influxdb_user/password y grafana_password). Si el usuario
        # rellena alguno, validamos aquí en cliente para no chocar con el 400.
        base_fields = {
            "influxdb_user": influx_user.strip(),
            "influxdb_password": influx_password.strip(),
            "grafana_password": grafana_password.strip(),
        }
        if any(base_fields.values()):
            missing = [field for field, value in base_fields.items() if not value]
            if missing:
                st.error(
                    "El componente 'base' requiere sus tres campos obligatorios. "
                    f"Faltan: {', '.join(missing)}."
                )
                return None
            infrastructure["component"] = {"base": base_fields}

    body: dict[str, Any] = {
        "infrastructure": infrastructure,
        "dataset": {"output": outputs},
        "auto_start_elcm": auto_start,
        "ephemeral_tn": ephemeral,
    }

    if exp_name.strip():
        body["experiment"] = {
            "name": exp_name.strip(),
            "testcase_paths": _lines_to_list(testcases),
            "ues_paths": _lines_to_list(ues),
        }

    if auto_start and "experiment" not in body:
        st.error("Con auto_start_elcm=True debes indicar experiment.name.")
        return None

    return body


def tab_new_execution() -> None:
    """Formulario que arma y envía POST /executions."""
    st.subheader("Nueva ejecución")
    with st.form("new_execution"):
        name = st.text_input("Nombre de la TN", value="tn-demo")
        col1, col2 = st.columns(2)
        with col1:
            lib_type = st.text_input("library_reference_type", value="branch")
        with col2:
            lib_value = st.text_input("library_reference_value", value="")

        st.markdown("**Componente base (monitorización)**")
        st.caption(
            "Campos obligatorios del componente 'base': si rellenas alguno, hacen "
            "falta los tres (influxdb_user, influxdb_password, grafana_password)."
        )
        influx_user = st.text_input("influxdb_user (obligatorio)", value="admin")
        influx_password = st.text_input(
            "influxdb_password (obligatorio)", value="", type="password"
        )
        grafana_password = st.text_input(
            "grafana_password (obligatorio)", value="", type="password"
        )

        outputs = st.multiselect("dataset.output", options=list(DATASET_OUTPUTS), default=["logs"])
        col3, col4 = st.columns(2)
        with col3:
            auto_start = st.checkbox("auto_start_elcm", value=True)
        with col4:
            ephemeral = st.checkbox("ephemeral_tn", value=False)

        st.markdown("**Experimento inicial** (obligatorio si auto_start_elcm)")
        exp_name = st.text_input("experiment.name", value="exp-demo")
        testcases = st.text_area("testcase_paths (uno por línea)", value="TestCase_ping.yml")
        ues = st.text_area("ues_paths (uno por línea)", value="")

        with st.expander("Avanzado: component como JSON (sobrescribe lo de arriba)"):
            component_json = st.text_area("component (JSON)", value="", height=160)

        submitted = st.form_submit_button("Lanzar ejecución")

    if not submitted:
        return

    client = _get_client()
    if client is None:
        return

    body = _build_execution_body(
        name=name,
        lib_type=lib_type,
        lib_value=lib_value,
        influx_user=influx_user,
        influx_password=influx_password,
        grafana_password=grafana_password,
        outputs=outputs,
        auto_start=auto_start,
        ephemeral=ephemeral,
        exp_name=exp_name,
        testcases=testcases,
        ues=ues,
        component_json=component_json,
    )
    if body is None:
        return

    with st.expander("Body enviado"):
        st.json(body)

    try:
        result = client.create_execution(body)
    except ApiError as exc:
        _show_api_error(exc)
        return

    execution_id = result.get("execution_id") if isinstance(result, dict) else None
    if execution_id:
        st.session_state["last_execution_id"] = execution_id
    st.success(f"Aceptada (202). execution_id: {execution_id}")
    st.json(result)


# ===== Pestaña: Estado =====


def tab_status() -> None:
    """Consulta estado resumido + detalle de una ejecución."""
    st.subheader("Estado de una ejecución")
    execution_id = st.text_input(
        "execution_id",
        value=st.session_state.get("last_execution_id", ""),
        key="status_execution_id",
    )
    if not st.button("Consultar"):
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

    cols = st.columns(2)
    cols[0].metric("Estado", str(status.get("status", "?")))
    cols[1].metric("tn_id", str(status.get("tn_id") or "—"))
    if status.get("message"):
        st.info(status["message"])
    st.markdown("**Detalle completo**")
    st.json(detail)


# ===== Pestaña: Experimento ELCM =====


def tab_elcm() -> None:
    """Lanza un experimento ELCM sobre una TN viva."""
    st.subheader("Lanzar experimento ELCM")
    with st.form("elcm_form"):
        execution_id = st.text_input(
            "execution_id", value=st.session_state.get("last_execution_id", "")
        )
        exp_name = st.text_input("experiment.name", value="exp-demo")
        testcases = st.text_area("testcase_paths (uno por línea)", value="TestCase_ping.yml")
        ues = st.text_area("ues_paths (uno por línea)", value="")
        outputs = st.multiselect("dataset.output", options=list(DATASET_OUTPUTS), default=["logs"])
        submitted = st.form_submit_button("Lanzar experimento")

    if not submitted:
        return

    client = _get_client()
    if client is None:
        return
    if not execution_id.strip() or not exp_name.strip():
        st.error("execution_id y experiment.name son obligatorios.")
        return
    if not outputs:
        st.error("Selecciona al menos un formato en dataset.output.")
        return

    body = {
        "experiment": {
            "name": exp_name.strip(),
            "testcase_paths": _lines_to_list(testcases),
            "ues_paths": _lines_to_list(ues),
        },
        "dataset": {"output": outputs},
    }
    try:
        result = client.start_elcm(execution_id.strip(), body)
    except ApiError as exc:
        _show_api_error(exc)
        return
    st.success("Experimento aceptado (202).")
    st.json(result)


# ===== Pestaña: Borrar TN =====


def tab_teardown() -> None:
    """Dispara el borrado de la Trial Network de una ejecución."""
    st.subheader("Borrar Trial Network")
    execution_id = st.text_input(
        "execution_id",
        value=st.session_state.get("last_execution_id", ""),
        key="teardown_execution_id",
    )
    confirm = st.checkbox("Confirmo el borrado de la TN")
    if not st.button("Borrar TN", disabled=not confirm):
        return

    client = _get_client()
    if client is None:
        return
    if not execution_id.strip():
        st.warning("Introduce un execution_id.")
        return

    try:
        result = client.delete_tn(execution_id.strip())
    except ApiError as exc:
        _show_api_error(exc)
        return
    st.success("Borrado lanzado (202).")
    st.json(result)


# ===== Entrada principal =====


def main() -> None:
    """Configura la página, el panel lateral y las pestañas de operación."""
    st.set_page_config(page_title="DaaS Orchestrator UI", page_icon="🛰️", layout="wide")
    st.title("DaaS Orchestrator")
    st.caption("UI para operar el orquestador sin Postman")

    render_sidebar()

    tabs = st.tabs(["Nueva ejecución", "Estado", "Experimento ELCM", "Borrar TN"])
    with tabs[0]:
        tab_new_execution()
    with tabs[1]:
        tab_status()
    with tabs[2]:
        tab_elcm()
    with tabs[3]:
        tab_teardown()


if __name__ == "__main__":
    main()
