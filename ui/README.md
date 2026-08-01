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
   - **Estado** → `GET /executions/{id}` y `/detail`
   - **Experimento ELCM** → `POST /executions/{id}/elcm`
   - **Borrar TN** → `DELETE /executions/{id}/tn`

## Notas

- No requiere CORS: las peticiones salen del servidor de Streamlit, no del
  navegador.
- Los campos que dejes vacíos se omiten del body (el backend rechaza strings
  vacíos), así evitas el error 400 de `empty_fields`.
- Para componentes distintos de `base`, usa el expander **Avanzado** de la
  pestaña *Nueva ejecución* y pega el bloque `component` como JSON.
