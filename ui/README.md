# UI web (Streamlit) — DaaS Orchestrator

Interfaz de navegador para operar el orquestador sin Postman. Es una app
**companion** independiente: habla con la API (FastAPI) por HTTP mediante
`httpx`, no importa ni comparte proceso con `app/`.

## Instalación

La UI es una dependencia opcional (no entra en el runtime del servicio):

```bash
pip install -e ".[ui]"
```

## Arranque

1. Arranca la API en un terminal:

   ```bash
   daas-orchestrator
   ```

2. Arranca la UI en otro terminal:

   ```bash
   streamlit run ui/streamlit_app.py
   ```

   Se abre en `http://localhost:8501`.

## Uso

1. En el panel lateral, pon la **Base URL** (por defecto `http://localhost:8000`)
   y tu **API key** (la misma `API_KEY` del `.env` del servidor).
2. Pulsa **Comprobar salud** para validar la conexión.
3. (Opcional) **Login (.env)** para refrescar el token TNLCM, o registra un
   usuario desde el expander.
4. Pestañas de operación:
   - **Nueva ejecución** → `POST /executions`
   - **Resumen** → `GET /executions/{id}/summary`
   - **Estado** → `GET /executions/{id}` y `/detail`
   - **Experimento ELCM** → `POST /executions/{id}/elcm`
   - **Borrar TN** → `DELETE /executions/{id}/tn`

## Pestaña «Resumen»

Es la vista pensada para el experimentador: qué ha pasado en cada fase, cuánto
ha tardado y dónde han quedado los resultados, sin vocabulario interno. Muestra

- KPIs de estado, duración total, red y experimentos correctos;
- una tabla de pasos (`Completado` / `Error` / `En curso` / `Pendiente` /
  `Omitido`) con su duración en lenguaje natural (`3 min 57 s`);
- el error traducido a una frase con sugerencia, si algo ha fallado;
- enlaces a los dashboards de Grafana y rutas de los resultados;
- un expander con el detalle técnico y otro con el informe en Markdown
  descargable (el mismo `summary.md` que el backend guarda en `artifacts/`).

El interruptor **Actualizar cada 5 s** hace que el panel se refresque solo
mientras la ejecución está en curso; un despliegue tarda unos 4-5 minutos y los
pasos van pasando de `Pendiente` a `En curso` y a `Completado`. Usa
`st.fragment`, así que el sondeo no reejecuta el resto de la app ni pierde lo
que hayas escrito en las otras pestañas.

Las etiquetas de los pasos llegan en inglés desde el backend a propósito (la
telemetría es internacional) y se pintan tal cual, para no duplicar el catálogo
que vive en `app/utils/execution_summary.py`. Ver `docs/TELEMETRY.md`.

## Notas

- No requiere CORS: las peticiones salen del servidor de Streamlit, no del
  navegador.
- Los campos que dejes vacíos se omiten del body (el backend rechaza strings
  vacíos), así evitas el error 400 de `empty_fields`.
- Para componentes distintos de `base`, usa el expander **Avanzado** de la
  pestaña *Nueva ejecución* y pega el bloque `component` como JSON.
