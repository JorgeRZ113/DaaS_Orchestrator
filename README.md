# tfgjorge

Orquestador API para ejecutar un flujo en dos fases:

1. `TNLCM`: despliegue y preparacion de Trial Network.
2. `ELCM`: ejecucion de experimentos y recogida de logs.

## Descripcion

Este servicio expone endpoints HTTP para crear ejecuciones, consultar su estado y recuperar detalle de resultados. El flujo objetivo es reproducible y guiado por descriptores y ejemplos en `examples`.

## Objetivo del proyecto

- Automatizar el pipeline `TNLCM -> VPN manual -> ELCM`.
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
TNLCM_TOKEN=changeme

ELCM_URL=http://ip.elcm:5001

REQUEST_TIMEOUT=60
POLL_INTERVAL=10
TNLCM_ACTIVATE_TIMEOUT=1800
TNLCM_ACTIVATE_RETRY_DELAY=5
TNLCM_ACTIVATE_REDEPLOY_MAX_ATTEMPTS=1
TNLCM_REDEPLOY_DELAY=5
TNLCM_RECOVERY_DESTROY_DELAY=0
TNLCM_REPORT_TIMEOUT=300

EXECUTIONS_FILE=./executions.json
ARTIFACTS_DIR=./artifacts
EXAMPLES_DIR=./examples
LOG_LEVEL=INFO
```

## `examples` y su uso en endpoints

| Archivo | Uso | Donde se referencia |
|---|---|---|
| `examples/tn_descriptor_elcm.yaml` | Descriptor TNLCM | `infrastructure.descriptor_path` en `POST /executions` o `POST /executions/tnlcm` |
| `examples/TestCase_ping.yml` | Test case para ELCM | `experiment.testcase_path` / `experiment.testcase_paths` en `POST /executions` o `POST /executions/tnlcm` |
| `examples/Exp_Desc.json` | Descriptor base para `/experiment/run` interno de ELCM | Cargado automaticamente durante fase ELCM |

Nota: En `Exp_Desc.json`, `TestCases` debe contener nombres logicos (ej. `Test_ping`), no rutas.

## Endpoints actuales (resumen)

Header requerido para endpoints de ejecucion:

- `x-api-key: <API_KEY>`

| Metodo | Endpoint | Auth | Body | Para que sirve |
|---|---|---|---|---|
| `GET` | `/health` | No | No | Verificar servicio |
| `POST` | `/executions` | Si | Si | Alias para iniciar TNLCM |
| `POST` | `/executions/tnlcm` | Si | Si | Iniciar fase TNLCM |
| `POST` | `/executions/{execution_id}/elcm` | Si | No | Iniciar fase ELCM |
| `GET` | `/executions/{execution_id}` | Si | No | Estado resumido |
| `GET` | `/executions/{execution_id}/detail` | Si | No | Estado detallado + artefactos |

## Endpoints actuales (detalle)

### `GET /health`
- Sin autenticacion.
- Respuesta: estado general del servicio.

### `POST /executions` (alias TNLCM)
- Inicia fase TNLCM.
- Equivalente funcional de `POST /executions/tnlcm`.

### `POST /executions/tnlcm`
- Inicia despliegue TNLCM.
- Devuelve `execution_id`, `status`, `message`.

### `POST /executions/{execution_id}/elcm`
- Inicia fase ELCM sobre una ejecucion existente.
- Requiere TNLCM completado y VPN manual activa.

### `GET /executions/{execution_id}`
- Devuelve estado resumido de ejecucion.

### `GET /executions/{execution_id}/detail`
- Devuelve detalle completo (incluye ids y artifacts).

## Payloads (A: minimo viable)

### Para `POST /executions` o `POST /executions/tnlcm`



Payload recomendado (con ejemplos de ficheros):

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
    "testcase_path": "TestCase_ping.yml",
    "testcase_paths": [
    ],
    "ues_paths": []
  },
  "dataset": {
    "output": "logs"
  }
}
```

### Para `POST /executions/{execution_id}/elcm`

- Sin body.
- Solo `execution_id` en path + header `x-api-key`.

## Flujo de uso recomendado

1. `POST /executions/tnlcm` con payload.
2. `GET /executions/{execution_id}` hasta `COMPLETED`.
3. Activar VPN manualmente.
4. `POST /executions/{execution_id}/elcm`.
5. Consultar resultado con `GET /executions/{execution_id}` o `GET /executions/{execution_id}/detail`.

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

## Uso con Postman

- Coleccion: `API_JSON/DaaS.postman_collection.json`
- Variables: `baseUrl`, `apiKey`, `executionId`

## Soporte

Si necesitas validar el flujo completo, usa primero la coleccion Postman y revisa el endpoint de detalle para inspeccionar artifacts y errores:

- `GET /executions/{execution_id}/detail`
