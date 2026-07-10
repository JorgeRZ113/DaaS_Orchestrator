# tfgjorge - DaaS Orchestrator

Orquestador API unificado para automatizar el pipeline completo de generación de datasets en redes 5G/6G:

1. **TNLCM** (Trial Network Lifecycle Manager): Despliegue y preparación automática de Trial Network
2. **VPN WireGuard**: Activación automática tras TNLCM, desactivación en cleanup
3. **ELCM** (Experiment Lifecycle Manager): Generación, subida y ejecución de test cases + experimentos

## Descripción General

Servicio HTTP que expone un **endpoint unificado `/executions`** para orquestar todo el flujo automáticamente (por defecto) o paso a paso (con control manual). 

El flujo es reproducible, guiado por:
- **Descriptores TNLCM** renderizados con `ytt` desde `templates/TNLCM/`
- **Templates ELCM** desde `templates/ELCM/`
- **Contrato de componentes** validado contra campos editables del overlay

## Objetivo del Proyecto

- **API unificada** (`POST /executions`) que ejecuta TNLCM + ELCM automático (defecto) o solo TNLCM con control manual
- **Validación centralizada** de payload con `extract_component_template_values()` que soporta formatos plano y anidado
- **Persistencia determinista**: `execution_id` derivado del `infrastructure.name`, reproducible
- **Reintentos automáticos**: Recuperación ante fallos transitorios de TNLCM (ej. `activate` con backoff)
- **VPN WireGuard automática**: Activación tras TNLCM, desactivación en cleanup ELCM
- **Telemetría granular**: Métricas por fase (TNLCM create/activate, ELCM total) con `execution_id` para correlación
- **Persistencia de artefactos**: `DatasetDescriptor` guardado inmediatamente en `artifacts/<execution_id>/`

## Requisitos

- Python `>=3.10`.
- Binario [`ytt`](https://carvel.dev/ytt/) `v0.55.1` instalado y accesible en el `PATH` (misma versión que instala el pipeline de CI, ver `.gitlab-ci.yml`).
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

## Estructura de Archivos Clave

| Ruta | Propósito |
|---|---|
| `templates/TNLCM/` | Templates renderizables con `ytt` para TNLCM. Base: `base_tnlcm_descriptor.yaml`; componentes: `<nombre>_sample_tnlcm_descriptor.yaml` |
| `templates/TNLCM/overlays/` | Overlays TNLCM que definen campos editables por template |
| `templates/ELCM/TestCase/` | Fragmentos y bases de TestCase para experimentos |
| `templates/ELCM/template_experiment_descriptor.json` | Descriptor base para `/experiment/run` dentro de ELCM |
| `app/utils/component_contract.py` | **NUEVO**: Extractor centralizado para normalizar y validar `component.<template>.<field>` |
| `app/orchestrator.py` | Orquestador principal: TNLCM + ELCM + WireGuard automático |
| `app/generators.py` | Generadores de descriptores TNLCM y ELCM con `ytt` |
| `app/artifacts.py` | Persistencia de ejecuciones y artefactos |
| `examples/` | Ejemplos de payloads y casos de uso |

**Nota importante**: En descriptores de experimento, `TestCases` debe contener nombres lógicos (ej. `testcase_001`), no rutas absolutas.

## Endpoints actuales (resumen)

Header requerido para endpoints de ejecucion:

- `x-api-key: <API_KEY>`

| Metodo | Endpoint | Auth | Body | Para que sirve |
|---|---|---|---|---|
| `GET` | `/health` | No | No | Verificar servicio |
| `POST` | `/refresh` | Si | No | Recarga en caliente variables mutables de `.env` |
| `POST` | `/register` | No | No (query params) | Registra usuario en TNLCM y devuelve access/refresh token |
| `POST` | `/executions` | Si | Si | **[UNIFICADO]** Ejecuta TNLCM + ELCM automático (o solo TNLCM si `auto_start_elcm=false`) |
| `POST` | `/login` | Si | No | Hacer login TNLCM con `.env` y guardar el token en memoria |
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

### `POST /refresh`
- Recarga sin reinicio solo configuracion mutable en memoria del proceso actual.
- Requiere header `x-api-key`.
- Valida tipos/rangos antes de aplicar cambios.
- No recarga: `APP_HOST`, `APP_PORT`, `EXECUTIONS_FILE`, `ARTIFACTS_DIR`, `EXAMPLES_DIR`.

### `POST /register`
- Registra un usuario en TNLCM y, tras registro exitoso, realiza login para devolver tokens.
- No requiere header `x-api-key`.
- Parámetros por query string: `username` (obligatorio), `password` (obligatorio), `email` (opcional), `org` (opcional).
- Envía al TNLCM el body JSON: `{"email":..., "username":..., "password":..., "org":...}` y luego hace login para obtener `access_token`/`refresh_token`.

### `POST /login`
- Usa `TNLCM_USER` y `TNLCM_PASSWORD` de `.env` para hacer login en TNLCM.
- Almacena `access_token` y `refresh_token` en memoria (módulo `tnlcm.py`).
- Los tokens en memoria se usan automáticamente en los headers Bearer de todas las llamadas TNLCM.
- Si no hay token en memoria, el sistema falla y te pide llamar a `POST /login`.
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

## Payloads: Formato y Validación

### Contrato Canónico del Payload

El formato canónico es: `component.<template>.<field> = value`

- `<template>`: Identificador del descriptor (`base`, `mongodb`, etc.)
- `<field>`: Nombre del campo editable (ej. `influxdb_user`)
- Los campos se validan contra el overlay TNLCM de cada template

**Ejemplo mínimo (autocompletar con defaults):**

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

**Ejemplo con componente base (formato plano recomendado):**

```json
{
  "infrastructure": {
    "name": "tn-demo-base",
    "descriptor_path": "base_tnlcm_descriptor.yaml",
    "component": {
      "base": {
        "influxdb_version": "2.7.11",
        "influxdb_user": "admin",
        "influxdb_password": "adminadmin",
        "influxdb_org": "testing",
        "influxdb_bucket": "testing",
        "influxdb_token": "default-token-testing",
        "grafana_version": "11.6.0",
        "grafana_password": "adminadmin",
        "prometheus_version": "2.54.3"
      }
    },
    "parameters": {
      "library_reference_type": "branch",
      "library_reference_value": "develop"
    }
  },
  "experiment": {
    "name": "exp-demo",
    "testcase_paths": ["TestCase_ping.yml"],
    "ues_paths": []
  },
  "dataset": {
    "output": "logs"
  },
  "auto_start_elcm": true
}
```

### Formatos Soportados de `component`

El validador centralizado (`app/utils/component_contract.py`) acepta dos formatos:

#### 1. **Formato Plano (CANÓNICO, Recomendado)**

```json
"component": {
  "base": {
    "influxdb_user": "admin",
    "influxdb_password": "secret",
    "grafana_version": "11.6.0"
  }
}
```

- Campos directos mapeados automáticamente a sus secciones en el overlay
- Más legible, menos anidación
- **Recomendado para nuevas integraciones**

#### 2. **Formato Anidado (RETROCOMPATIBILIDAD)**

```json
"component": {
  "base": {
    "monitoring": {
      "influxdb_user": "admin",
      "influxdb_password": "secret",
      "grafana_version": "11.6.0"
    }
  }
}
```

- Agrupa campos por secciones del overlay (ej. `monitoring`, `storage`)
- Soportado para compatibilidad con payloads antiguos
- Convertido internamente a formato plano durante extracción

### Validación de Componentes

Durante `POST /executions`:

1. Se resuelve el template TNLCM para el componente (ej. `base_tnlcm_descriptor.yaml`)
2. Se carga el overlay TNLCM del template para obtener campos editables
3. Se usa `extract_component_template_values()` para normalizar y validar campos
4. Se rechazan con error `400` los campos no editables

**Errores posibles:**

```json
{
  "detail": {
    "invalid_fields": [
      "component.base.unknown_field: field not allowed",
      "component.mongodb.missing_template: template not found",
      "component.redis.storage.unknown: field not allowed"
    ]
  }
}
```

### Campo `auto_start_elcm`

- `true` (defecto): Ejecuta automáticamente TNLCM + ELCM secuencial
- `false`: Solo ejecuta TNLCM, requiere llamar manualmente a `POST /executions/{execution_id}/elcm`

## Flujo de uso recomendado

### Opción 1: Automático (Recomendado)

Con VPN automática ya solucionada, la forma más sencilla es:

```
1. POST /login  (opcional pero recomendado)
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
1. POST /login  (opcional pero recomendado)
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
- `POST /login` con header `x-api-key`.
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

## Arquitectura de Componentes y Validación

### Extractor Centralizado (`app/utils/component_contract.py`)

El módulo `extract_component_template_values()` centraliza la lógica de:
1. **Normalización**: Convierte formato plano → estrutura sección/campo
2. **Validación**: Verifica contra campos editables del overlay TNLCM
3. **Reutilización**: Se usa en `app/main.py` (validación) y `app/generators.py` (extracción durante render)

**Consumidores:**

| Módulo | Uso |
|---|---|
| `app/main.py` | Validación en `_validate_components_or_raise()` dentro de `POST /executions` |
| `app/generators.py` | Extracción de campos editables antes de render `ytt` en `generate_tnlcm_descriptor()` |

**Ventajas de centralización:**
- Evita duplicación de lógica validación ↔ generación
- Garantiza comportamiento consistente en ambos puntos
- Facilita mantenimiento y pruebas

### Overlays TNLCM

Cada template TNLCM tiene un overlay que define qué campos pueden ser editados por el payload:

- **Ubicación**: `templates/TNLCM/overlays/<template_name>.yaml`
- **Estructura**: Define secciones y campos editables (ej. `monitoring: [influxdb_user, influxdb_password, ...]`)
- **Carga**: Automática mediante `overlay_editable_fields_for_template()` en `app/utils/ytt_renderer.py`

## Automatizacion WireGuard

- Implementacion en `app/utils/wireguard.py`.
- Helper de sistema en `app/utils/wireguard_helper.py`.
- El contenido de la VPN se toma del campo `wireguard_client_config` del reporte TNLCM, sin parseos extra.
- El archivo de interfaz se guarda como `<tn_id>.conf`.
- Linux: usa `wg-quick` y si la interfaz ya existe se actualiza (`down` + `up`).
- Windows: usa `wireguard.exe` via helper; primero intenta sin elevacion y, si detecta permisos insuficientes, reintenta elevando el helper.
- Campos de seguimiento en `GET /executions/{execution_id}/detail`: `vpn_interface`, `vpn_conf_path`, `vpn_status`, `vpn_error`.

## Estructura del resumen TNLCM

El artefacto `tnlcm_report_summary.json` se genera a partir del markdown devuelto por el endpoint externo de TNLCM.
La estructura actual respeta el esquema de `Modificacion_report.txt` y resume el reporte en estas claves:

- `tn_id`
- `summary.private_ssh_key`
- `summary.wireguard_client_config`
- `summary.tn_vxlan`
- `summary.tn_bastion`
- `summary.technitium_dns`
- `summary.monitoring`
- `summary.elcm`
- `summary.components`
- `summary.components_count`

Reglas de interpretación:

- Las claves fijas en `summary` se inicializan a `null` si no aparecen en el markdown.
- Los componentes adicionales se añaden en `summary.components`, respetando el orden del reporte.
- `components_count` cuenta todos los bloques de componentes detectados.
- Cada componente puede incluir `name`, `ip`, `ips`, `ports`, `credentials` y `extra_info` con metadatos adicionales.

## Changelog (formato compacto)

| Fecha | Cambio |
|---|---|
| 2026-03 | Persistencia de ejecuciones en `executions.json` |
| 2026-03 | `execution_id` determinista (`infrastructure.name`) |
| 2026-03 | Fases separadas: `POST /executions/tnlcm` y `POST /executions/{execution_id}/elcm` |
| 2026-03 | ELCM sin token (`Authorization` eliminado) |
| 2026-03 | Uso de `templates/ELCM/template_experiment_descriptor.json` como base para `/experiment/run` |
| 2026-03 | Soporte de `ExecutionId` de ELCM (incluyendo entero) |
| 2026-03 | Polling de estado ELCM cada 10s |
| 2026-03 | Recuperacion automatica TNLCM ante error transitorio en `activate` |
| 2026-03 | Endpoint `/login` para login TNLCM con credenciales `.env` |
| 2026-03 | Tokens TNLCM almacenados en memoria (refresco explícito con `/login`) |
| 2026-03 | Si TN ya existe en estado "activated", se salta create y continua con activate |
| 2026-03 | URL de ELCM extraída dinámicamente del reporte TNLCM |
| 2026-04 | Automatizacion de WireGuard: `<tn_id>.conf`, activacion automatica tras TNLCM y desactivacion obligatoria en cleanup ELCM |
| 2026-04 | Refactor de WireGuard a `app/utils` con helper dedicado `app/utils/wireguard_helper.py` |
| 2026-05 | **Telemetría Orchestrator-Céntrica**: Métricas granulares de fase (TNLCM create/activate, ELCM total, ejecución end-to-end) |
| 2026-05 | **API Unificada**: Endpoint `/executions` unificado con `auto_start_elcm` (defecto `true`) para flujo automático TNLCM+ELCM |
| 2026-05 | Rediseño del resumen TNLCM: claves fijas `tn_init`/`monitoring`/`elcm` y componentes auxiliares ordenados |
| 2026-05-17 | Convertidos templates TNLCM base a `ytt` (`@ytt:data` / `@data.values`) y añadida documentación sobre qué valores debe contener el `DataDescriptor` |
| **2026-05-30** | **Extractor Centralizado**: Módulo `app/utils/component_contract.py` con `extract_component_template_values()` para normalizar y validar campos `component.<template>.<field>` contra editables del overlay; soporta formatos plano y anidado con retrocompatibilidad |

## Uso con Postman

- Coleccion: `API_JSON/DaaS.postman_collection.json`
- Variables: `baseUrl`, `apiKey`, `executionId`

## Debugging y Tests

Ejecutar suite completa de tests (74 tests):

```bash
python -m pytest tests/ -v
```

Ejecutar tests de un módulo específico:

```bash
python -m pytest tests/test_main_endpoints.py -v
python -m pytest tests/test_generators.py -v
```

Los tests validan:
- Validación de payload con `extract_component_template_values()`
- Generación de descriptores TNLCM con múltiples componentes
- Flujos de orquestación TNLCM + ELCM
- Reintentos y recuperación ante fallos

## Información Sobre Documentación del Proyecto

### Documentación Vigente

- **Este README**: Punto de entrada principal, contrato de API y payloads actualizados
- **`docs/INFORME_TNLCM.md`**: Guía rápida para ejecutar pruebas TNLCM
- **`docs/INFORME_ELCM.md`**: Guía rápida para ejecutar pruebas ELCM
- **`docs/CI_CD_VARIABLES.md`**: Variables de deployment y configuración
- **`Recursos/300526_Resumen.md`**: Documentación técnica detallada archivo por archivo (SI EXISTE)

### Documentación Legacy (NO Usar)

Los siguientes archivos contienen snapshots históricos de fases anteriores del desarrollo. **NO se recomiendan para nuevas implementaciones**:

- `docs/UNIFIED_EXECUTIONS_API.md` - Documentación de fase de unificación (desalineada con estado actual)
- `docs/TELEMETRY.md` - Telemetría anterior a refactor (módulo movido a `app/utils/telemetry.py`)
- `docs/TNLCM_MASKS_SUMMARY.md` - Modelo de máscaras TNLCM obsoleto
- `docs/TNLCM_MASKS_INTEGRATION.md` - Propuesta de arquitectura superada
- `docs/CHANGES_UNIFIED_EXECUTIONS_API.md` - Changelog de fase de unificación
- `docs/RESUMEN_IMPLEMENTACION_API_UNIFICADA.md` - Resumen histórico
- `docs/TELEMETRY_REFACTOR.md` - Changelog de refactor de telemetría
- `docs/CHANGELOG_TELEMETRY_REFACTOR.md` - Más changelog histórico
- `docs/RESUMEN_VISUAL.txt` - Snapshot visual histórico
- `docs/INDEX_MASKS.md` - Índice vacío

**Recomendación:** Si necesitas historiacomprender las decisiones de diseño, revisá `Recursos/` donde se centraliza la documentación técnica en profundidad.

## Soporte

Si necesitas validar el flujo completo, usa primero la coleccion Postman y revisa el endpoint de detalle para inspeccionar artifacts y errores:

- `GET /executions/{execution_id}/detail`

Para problemas de validación de componentes, consulta la sección "Validación de Componentes" arriba y revisa los overlays TNLCM en `templates/TNLCM/overlays/`.

Para entender en detalle la arquitectura del extractor de componentes, ver `app/utils/component_contract.py` y sus consumidores en `app/main.py` y `app/generators.py`.

