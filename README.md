# tfgjorge

Orquestador API para ejecutar un flujo en dos fases:

1. `TNLCM`: despliegue y preparacion de Trial Network.
2. `ELCM`: ejecucion de experimentos y recogida de logs.

## Descripcion

Este servicio expone endpoints HTTP para crear ejecuciones, consultar su estado y recuperar detalle de resultados. El flujo objetivo es reproducible y guiado por descriptores y ejemplos en `examples`.

## Objetivo del proyecto

- Automatizar el pipeline `TNLCM -> WireGuard -> ELCM`.
- Mantener `execution_id` determinista y persistente.
- Mejorar robustez con reintentos y recuperacion automatica.
- Dejar trazabilidad de estados y artefactos.

## Requisitos

- Python `>=3.10`.
- Dependencias definidas en `pyproject.toml`.
- Acceso de red a TNLCM y ELCM configurados en `.env`.

## Instalacion rapida

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

## Ejecucion

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Configuracion `.env`

Puedes usar `/.env.example` como plantilla.

```dotenv
APP_ENV=dev
APP_HOST=0.0.0.0
APP_PORT=8000

API_KEY=changeme

TNLCM_URL=http://ip.elcm:5000/
TNLCM_USER=changeme
TNLCM_PASSWORD=changeme

# ELCM URL se resuelve dinámicamente desde el reporte TNLCM

EXECUTIONS_FILE=./executions.json
ARTIFACTS_DIR=./artifacts
EXAMPLES_DIR=./examples
LOG_LEVEL=INFO
TELEMETRY_REPORT_ARTIFACTS=true
```

Nota: la ventana máxima para disparar `POST /executions/{execution_id}/elcm` tras completar TNLCM se define como constante interna en `app/orchestrator.py` (`ELCM_START_TIMEOUT_SECONDS`). Si se supera, el orquestador cancela la ejecución y ejecuta `destroy/purge` automático. **Con `auto_start_elcm=true` (defecto), esto no es un problema ya que ELCM se dispara automáticamente.**

## `examples` y su uso en endpoints

| Archivo | Uso | Donde se referencia |
|---|---|---|
| `examples/tn_descriptor_elcm.yaml` | Descriptor TNLCM | `infrastructure.descriptor_path` en `POST /executions` |
| `examples/TestCase_ping.yml` | Test case para ELCM | `experiment.testcase_paths` en `POST /executions` |
| `examples/Exp_Desc.json` | Descriptor base para `/experiment/run` interno de ELCM | Cargado automáticamente durante fase ELCM |
| `examples/EXECUTIONS_EXAMPLES.md` | Ejemplos completos de payloads y flujos | Referencia de uso de la API |

Nota: En `Exp_Desc.json`, `TestCases` debe contener nombres lógicos (ej. `Test_ping`), no rutas.

## Endpoints actuales (resumen)

Header requerido para endpoints de ejecucion:

- `x-api-key: <API_KEY>`

| Metodo | Endpoint | Auth | Body | Para que sirve |
|---|---|---|---|---|
| `GET` | `/health` | No | No | Verificar servicio |
| `POST` | `/config/reload` | Si | No | Recarga en caliente variables mutables de `.env` |
| `POST` | `/executions` | Si | Si | **[UNIFICADO]** Ejecuta TNLCM + ELCM automático (o solo TNLCM si `auto_start_elcm=false`) |
| `POST` | `/tnlcm/token/refresh` | Si | No | Hacer login TNLCM con `.env` y guardar el token en memoria |
| `POST` | `/executions/{execution_id}/elcm` | Si | No | Disparar ELCM manualmente (para `auto_start_elcm=false`) |
| `GET` | `/executions/{execution_id}` | Si | No | Estado resumido |
| `GET` | `/executions/{execution_id}/detail` | Si | No | Estado detallado + artefactos |

## Endpoints actuales (detalle)

### `GET /health`
- Sin autenticacion.
- Respuesta: estado general del servicio.

### `POST /executions` (UNIFICADO)

- **Punto de entrada único** para toda ejecución.
- Ejecuta TNLCM automáticamente.
- Si `auto_start_elcm=true` (defecto), ejecuta ELCM automáticamente al completar TNLCM.
- Si `auto_start_elcm=false`, solo ejecuta TNLCM y requiere llamar a `POST /executions/{execution_id}/elcm` manualmente.
- Devuelve `execution_id`, `status`, `message`.
- Ver `docs/UNIFIED_EXECUTIONS_API.md` y `examples/EXECUTIONS_EXAMPLES.md` para detalles y ejemplos.

### `POST /config/reload`
- Recarga sin reinicio solo configuracion mutable en memoria del proceso actual.
- Requiere header `x-api-key`.
- Valida tipos/rangos antes de aplicar cambios.
- No recarga: `APP_HOST`, `APP_PORT`, `EXECUTIONS_FILE`, `ARTIFACTS_DIR`, `EXAMPLES_DIR`.

### `POST /tnlcm/token/refresh`
- Usa `TNLCM_USER` y `TNLCM_PASSWORD` de `.env` para hacer login en TNLCM.
- Almacena `access_token` y `refresh_token` en memoria (módulo `tnlcm.py`).
- Los tokens en memoria se usan automáticamente en los headers Bearer de todas las llamadas TNLCM.
- Si no hay token en memoria, el sistema falla y te pide llamar a `POST /tnlcm/token/refresh`.
- Si TNLCM no responde en 20 segundos (por ejemplo, VPN no activa), devuelve `504` con aviso para revisar la VPN.
- No requiere body.

### `POST /executions/{execution_id}/elcm`
- Inicia fase ELCM sobre una ejecucion existente.
- Requiere TNLCM completado.
- La VPN WireGuard se activa automaticamente al finalizar TNLCM y se desactiva en el cleanup de ELCM.

### `GET /executions/{execution_id}`
- Devuelve estado resumido de ejecucion.

### `GET /executions/{execution_id}/detail`
- Devuelve detalle completo (incluye ids y artifacts).

## Payloads (A: minimo viable)

### Para `POST /executions` (Nuevo: Endpoint Unificado)

Payload mínimo (ejecución completa automática):

```json
{
  "infrastructure": {
    "name": "tn-demo"
  },
  "experiment": {
    "name": "exp-001",
    "testcase_paths": ["TestCase_ping.yml"],
    "ues_paths": []
  }
}
```

Payload recomendado (con control manual):

```json
{
  "infrastructure": {
    "name": "tn-demo-manual",
    "descriptor_path": "tn_descriptor_elcm.yaml",
    "parameters": {
      "library_reference_type": "branch",
      "library_reference_value": "develop"
    }
  },
  "experiment": {
    "name": "exp-demo",
    "testcase_paths": [
      "TestCase_ping.yml"
    ],
    "ues_paths": []
  },
  "dataset": {
    "output": "logs"
  },
  "auto_start_elcm": true
}
```

**Nuevo campo `auto_start_elcm`:**
- `true` (defecto): TNLCM + ELCM automático secuencial
- `false`: Solo TNLCM, luego disparar ELCM manualmente con `POST /executions/{execution_id}/elcm`

### Para `POST /executions/{execution_id}/elcm`

- Sin body.
- Solo `execution_id` en path + header `x-api-key`.

### Para `POST /tnlcm/token/refresh`

- Sin body.
- Solo header `x-api-key`.
- Devuelve preview del token (no expone el token completo).
- Los tokens obtenidos se guardan en memoria y se usan automáticamente en todas las llamadas TNLCM posteriores.

## Flujo de uso recomendado

### Opción 1: Automático (Recomendado)

Con VPN automática ya solucionada, la forma más sencilla es:

```
1. POST /tnlcm/token/refresh  (opcional pero recomendado)
2. POST /executions con auto_start_elcm=true (defecto)
3. GET /executions/{execution_id} para monitorear
4. ✓ TNLCM + ELCM automático secuencial
```

**Ventajas:**
- Más simple (un único POST)
- Sin esperas manuales
- Flujo reproducible

### Opción 2: Control Manual (Legacy)

Si necesitas control granular:

```
1. POST /tnlcm/token/refresh  (opcional pero recomendado)
2. POST /executions con auto_start_elcm=false
3. GET /executions/{execution_id} hasta COMPLETED (TNLCM)
4. POST /executions/{execution_id}/elcm  (disparar ELCM)
5. GET /executions/{execution_id} hasta COMPLETED (ELCM)
```

**Ventajas:**
- Control paso-a-paso
- Inspeccionar estado entre fases
- Debuggear más fácilmente

### Detalles del Flujo Automático

**Autenticacion TNLCM (opcional pero recomendado):**
- `POST /tnlcm/token/refresh` con header `x-api-key`.
  - Obtiene `access_token` de TNLCM y lo guarda en memoria.
  - Los tokens en memoria tendrán prioridad en todas las llamadas TNLCM.
  - Si NO ejecutas este endpoint, las llamadas TNLCM fallarán hasta refrescar el token.

**Orden de ejecución automático:**
1. `POST /executions` con `auto_start_elcm=true` (defecto)
2. Sistema descarga el reporte TNLCM y guarda la interfaz WireGuard en `ARTIFACTS_DIR/<execution_id>/<tn_id>.conf`.
3. Sistema activa automáticamente el túnel WireGuard usando `tn_id` como nombre de túnel.
4. **Al completar TNLCM exitosamente:** Sistema dispara automáticamente ELCM (si `auto_start_elcm=true`)
5. ELCM: upload testcases → run experiment → collect logs
6. En cleanup de ELCM se desactiva obligatoriamente el túnel WireGuard.
7. Se destruye la TN.
8. Ejecución completada.

## Automatizacion WireGuard

- Implementacion en `app/utils/wireguard.py`.
- Helper de sistema en `app/utils/wireguard_helper.py`.
- El contenido de la VPN se toma del campo `wireguard_client_config` del reporte TNLCM, sin parseos extra.
- El archivo de interfaz se guarda como `<tn_id>.conf`.
- Linux: usa `wg-quick` y si la interfaz ya existe se actualiza (`down` + `up`).
- Windows: usa `wireguard.exe` via helper; primero intenta sin elevacion y, si detecta permisos insuficientes, reintenta elevando el helper.
- Campos de seguimiento en `GET /executions/{execution_id}/detail`: `vpn_interface`, `vpn_conf_path`, `vpn_status`, `vpn_error`.

## Changelog (formato compacto)

| Fecha | Cambio |
|---|---|
| 2026-03 | Persistencia de ejecuciones en `executions.json` |
| 2026-03 | `execution_id` determinista (`infrastructure.name`) |
| 2026-03 | Fases separadas: `POST /executions/tnlcm` y `POST /executions/{execution_id}/elcm` |
| 2026-03 | ELCM sin token (`Authorization` eliminado) |
| 2026-03 | Uso de `examples/Exp_Desc.json` como base para `/experiment/run` |
| 2026-03 | Soporte de `ExecutionId` de ELCM (incluyendo entero) |
| 2026-03 | Polling de estado ELCM cada 10s |
| 2026-03 | Recuperacion automatica TNLCM ante error transitorio en `activate` |
| 2026-03 | Endpoint `/tnlcm/token/refresh` para login TNLCM con credenciales `.env` |
| 2026-03 | Tokens TNLCM almacenados en memoria (refresco explícito con `/tnlcm/token/refresh`) |
| 2026-03 | Si TN ya existe en estado "activated", se salta create y continua con activate |
| 2026-03 | URL de ELCM extraída dinámicamente del reporte TNLCM |
| 2026-04 | Automatizacion de WireGuard: `<tn_id>.conf`, activacion automatica tras TNLCM y desactivacion obligatoria en cleanup ELCM |
| 2026-04 | Refactor de WireGuard a `app/utils` con helper dedicado `app/utils/wireguard_helper.py` |
| 2026-05 | **Telemetría Orchestrator-Céntrica**: Métricas granulares de fase (TNLCM create/activate, ELCM total, ejecución end-to-end) |
| 2026-05 | **API Unificada**: Endpoint `/executions` unificado con `auto_start_elcm` (defecto `true`) para flujo automático TNLCM+ELCM; remover `/executions/tnlcm` redundante |

## Uso con Postman

- Coleccion: `API_JSON/DaaS.postman_collection.json`
- Variables: `baseUrl`, `apiKey`, `executionId`

## Soporte

Si necesitas validar el flujo completo, usa primero la coleccion Postman y revisa el endpoint de detalle para inspeccionar artifacts y errores:

- `GET /executions/{execution_id}/detail`
