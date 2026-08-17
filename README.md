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
- **Resumen legible**: `GET /executions/{id}/summary` y `summary.md`/`summary.json` traducen la telemetría a pasos, duraciones y errores en lenguaje natural para el experimentador ([`docs/TELEMETRY.md`](docs/TELEMETRY.md))
- **Persistencia de artefactos**: `DatasetDescriptor` guardado inmediatamente en `artifacts/<execution_id>/`
- **Arquitectura por capas y suite verificable**: `app/` separado en `api`/`services`/`adapters`/`rendering`/`storage`/`domain`/`core`, con 365 pruebas clasificadas por nivel, puerta de cobertura en CI y pruebas de mutación sobre la política de reintentos

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

## Pruebas y calidad

La suite esta organizada por **niveles**, y el nivel lo marca el directorio: no
hay que anotar nada en cada fichero, `tests/conftest.py` deriva el marcador de la
carpeta en la que vive la prueba.

| Nivel | Casos | Que ejercita | Tiempo |
|---|---:|---|---:|
| `unit` | 70 | Logica pura: sin red, sin disco | **0,6 s** |
| `integration` | 37 | Varios modulos, disco y binario `ytt` | 12 s |
| `adapters` | 44 | Contrato HTTP con TNLCM/ELCM/InfluxDB (transporte simulado) | 0,8 s |
| `api` | 76 | Endpoints via `TestClient` | 1,2 s |
| `contract` | 99 | Los ficheros de `templates/ELCM/TestCase/` contra las reglas de ELCM | 1 s |
| `system` | 38 | Ciclo de vida completo con dobles solo en los bordes | 4 s |

```bash
pip install -e ".[dev]"

python -m pytest -m unit          # ciclo corto de desarrollo (no necesita ytt)
python -m pytest                  # suite completa
python -m pytest --cov=app --cov-report=term-missing
```

**Puerta de calidad en CI** (`.gitlab-ci.yml`): `ruff check app tests`,
`black --check app tests`, una etapa `unit` que corta el pipeline en segundos sin
descargar el binario `ytt`, y la suite completa con `--cov-fail-under=75`
(cobertura actual: 77,5 %).

**Pruebas de mutacion** (opcional, no corre en CI): miden si los `assert`
detectan cambios reales del codigo, no solo si la linea se ejecuta. La
herramienta introduce alteraciones pequenas en el fuente (invertir una
comparacion, cambiar un operador, tocar una constante) y cuenta cuantas detecta
la suite; las que sobreviven senalan una asercion que falta.

```bash
pip install -e ".[mutation]"
cosmic-ray init mutation.toml .mutation/session.sqlite
cosmic-ray exec mutation.toml .mutation/session.sqlite
cr-report .mutation/session.sqlite
```

Se usa `cosmic-ray` y no `mutmut` porque este ultimo no soporta Windows nativo
(exigiria WSL). [`mutation.toml`](mutation.toml) apunta **solo** a
`app/core/retry.py`: mutar codigo que las unitarias no ejercitan produce
supervivientes por falta de cobertura, no de aserciones, y eso ensucia la senal.
La corrida completa tarda unos 9 min.

Resultado **[medido 2026-08-11]**: de 255 mutantes, 70,6 % -> 79,6 % bruto tras
anadir 7 pruebas; descontados los 52 equivalentes, los supervivientes reales
pasaron de 23 a 0. Encontro cuatro carencias en codigo que la cobertura daba por
verificado al 97 %, entre ellas que el catalogo de politicas de reintento
(codigos reintentables, numero de intentos) no estaba fijado por ninguna prueba.

La estrategia completa, con la clasificacion de los 365 casos y las mediciones,
esta en [`docs/VERIFICACION_Y_VALIDACION.md`](docs/VERIFICACION_Y_VALIDACION.md)
(las pruebas de mutacion, en su §6.4).

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

Nota: los tiempos de ELCM se definen como constantes internas en `app/services/orchestrator.py`: `ELCM_POLL_INTERVAL_SECONDS` (sondeo de estado) y `ELCM_EXECUTION_TIMEOUT_SECONDS` (timeout del experimento). La TN **no** se destruye por defecto al terminar: queda viva (`TN_READY`) hasta un `DELETE /executions/{id}/tn` o, si `ephemeral_tn=true`, tras el primer experimento.

## Estructura de Archivos Clave

| Ruta | Propósito |
|---|---|
| `templates/TNLCM/` | Templates renderizables con `ytt` para TNLCM. Base: `base_tnlcm_descriptor.yaml`; componentes: `<nombre>_sample_tnlcm_descriptor.yaml` |
| `templates/TNLCM/overlays/` | Overlays TNLCM que definen campos editables por template |
| `templates/ELCM/TestCase/` | **Biblioteca de TestCases y UEs**: es lo que `testcase_paths`/`ues_paths` resuelven por nombre de fichero, y de donde se suben a ELCM tal cual |
| `templates/ELCM/templates/` + `templates/ELCM/overlays/` | TestCases de dataset renderizables con `ytt`: `prometheus_to_csv_dataset` (salida `csv`) y `prometheus_to_grafana_dashboard` (salida `dashboard`) |
| `templates/ELCM/template_experiment_descriptor.json` | Plantilla base del Experiment Descriptor; se rellena por experimento (UEs + TestCases) y se genera en `artifacts/<id>/archivos_generados/` |
| `app/main.py` | Raíz de composición: monta los routers y expone `app` |
| `app/api/` | Capa HTTP: `routers/` (health, auth, admin, executions, experiments), `deps.py`, `phases.py` (desenlace de fase → código HTTP), `validation.py`, `errors.py` |
| `app/services/orchestrator.py` | Coordinador fino: arranca fases y expone el ciclo de vida a la API |
| `app/services/state.py` · `phases/` | Estado y persistencia de las ejecuciones; una fase por módulo (`tnlcm`, `elcm`, `teardown`) más `results.py` con la recolección del dataset |
| `app/adapters/tnlcm.py` / `app/adapters/elcm.py` | Adaptadores HTTP a TNLCM y ELCM |
| `app/observability/health.py` | Health de servicios (`/health/services`) y componentes (`/health/components`) |
| `app/rendering/` | `paths.py` (resolucion de plantillas), `overlays.py` (registro y campos editables), `ytt.py` (binario ytt), `yaml_style.py`; generadores en `tnlcm/overlay.py` + `tnlcm/renderer.py` (descriptor TNLCM) y `elcm/dataset.py` (TestCases de dataset csv/dashboard) |
| `app/storage/artifacts.py` | Persistencia de ejecuciones y artefactos (incluye carpeta `result/`) |
| `app/core/` · `app/domain/` | Config y reintentos (`config.py`, `retry.py`); modelos (`enums.py`, `descriptor.py`, `execution.py`, `component_contract.py`) |
| `app/api/schemas/` | Contratos HTTP de entrada (`requests.py`) y salida (`responses.py`) |
| `app/storage/` · `app/observability/` | `results_bundle.py` (extrae el CSV del ZIP de resultados); `telemetry.py`, `execution_summary.py`, `health.py` |
| `artifacts/<execution_id>/` | Artefactos de la ejecución: descriptor, reportes TNLCM, `<tn_id>.conf` y `result/` con las salidas del dataset |
| `tests/` | Suite por niveles: `unit/`, `integration/`, `adapters/`, `api/`, `contract/`, `system/`, más `conftest.py` con las fixtures compartidas |
| `examples/` | Ejemplos de payloads y el descriptor de infraestructura que resuelve `infrastructure.descriptor_path` (`EXAMPLES_DIR`). Los TestCases/UEs **ya no se resuelven aquí**: viven en `templates/ELCM/TestCase/` |

**Nota importante**: En descriptores de experimento, `TestCases` debe contener nombres lógicos (ej. `testcase_001`), no rutas absolutas.

## Endpoints actuales (resumen)

Header requerido para endpoints de ejecucion:

- `x-api-key: <API_KEY>`

| Metodo | Endpoint | Auth | Body | Para que sirve |
|---|---|---|---|---|
| `GET` | `/health/services` | No | No | Liveness del orquestador (DaaS) y de TNLCM |
| `GET` | `/health/components` | Si | No | Health HTTP de InfluxDB/Grafana/Prometheus/ELCM |
| `POST` | `/refresh` | Si | No | Recarga en caliente variables mutables de `.env` |
| `POST` | `/register` | No | No (query params) | Registra usuario en TNLCM y devuelve access/refresh token |
| `POST` | `/executions` | Si | Si | **[UNIFICADO]** Ejecuta TNLCM + ELCM automático (o solo TNLCM si `auto_start_elcm=false`) |
| `POST` | `/login` | Si | No | Hacer login TNLCM con `.env` y guardar el token en memoria |
| `POST` | `/executions/{execution_id}/elcm` | Si | Si (`experiment` + `dataset`) | Lanza un experimento sobre la TN viva (repetible, nombre único, salida de datos por experimento) |
| `DELETE` | `/executions/{execution_id}/tn` | Si | No | Borrado manual de la TN (deleted + purged) |
| `GET` | `/executions/{execution_id}` | Si | No | Estado resumido |
| `GET` | `/executions/{execution_id}/detail` | Si | No | Estado detallado + artefactos + `tn_state` en vivo desde TNLCM |
| `GET` | `/executions/{execution_id}/summary` | Si | No | Resumen legible para experimentadores (`?format=markdown` para texto) |

## Endpoints actuales (detalle)

### `GET /health/services`
- Sin autenticacion.
- Liveness del propio orquestador (DaaS) y de TNLCM. `status`: `ok` | `fallen` (solo un error de conexión/timeout marca TNLCM caído).

### `GET /health/components`
- Requiere header `x-api-key`.
- Health HTTP de los servicios fijos monitorizables (InfluxDB, Grafana, Prometheus, ELCM), según el diccionario estático de `app/observability/health.py`.

### `POST /executions` (UNIFICADO)

- **Punto de entrada único** para toda ejecución.
- Ejecuta TNLCM automáticamente.
- Si `auto_start_elcm=true` (defecto), ejecuta ELCM automáticamente al completar TNLCM.
- Si `auto_start_elcm=false`, solo ejecuta TNLCM y requiere llamar a `POST /executions/{execution_id}/elcm` manualmente.
- Devuelve `execution_id`, `status`, `message`.
- `dataset.output` define el/los formato(s) de entrega (ver sección "Campo `dataset.output`").
- Ver la colección Postman `API_JSON/DaaS.postman_collection.json` y la sección "Ciclo de vida de la TN" para el detalle del flujo.

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
- Lanza un experimento ELCM sobre la Trial Network ya desplegada y **viva** (estado `TN_READY`).
- Body: `{"experiment": {"name": "...", "testcase_paths": [...], "ues_paths": [...]}, "dataset": {"output": [...]}}`.
- **`dataset` por experimento**: cada llamada puede pedir una salida de datos distinta (`logs`/`csv`/`dashboard`/`raw`). Si se omite, por defecto `logs`. Las respuestas se guardan en `artifacts/<execution_id>/result/<experimento>/` (una subcarpeta por nombre de experimento).
- **Experiment Descriptor generado**: la lista de UEs y TestCases se rellena a partir del `experiment` y el descriptor se genera por ejecución (JSON, sin `ytt`), guardándose junto al TN Descriptor en `artifacts/<execution_id>/archivos_generados/experiment_descriptor_<experimento>.json`. Los ficheros de TestCases y UEs se toman de `templates/ELCM/TestCase/`.
- Repetible tantas veces como experimentos quieras (uno a la vez); cada experimento debe tener un **nombre único** dentro de la TN.
- Respuestas: `202` aceptado; `404` la ejecución no existe; `409` hay un experimento en curso, la TN no está lista o el nombre está repetido.

> **`experiment.name` es la clave del experimento dentro de la TN, no un simple rótulo.** La unicidad la impone **nuestro orquestador**, no ELCM. ELCM no exige nombres únicos. Aquí reutilizamos ese nombre para dos cosas dentro de la misma TN:
> 1. **Tracking de estado** — el `ExperimentRun` se localiza por `name`, un nombre repetido apuntaría al run equivocado y corrompería su estado/`finished_at`.
> 2. **Carpeta de resultados** — el dataset se guarda en `artifacts/<execution_id>/result/<experimento>/`, reutilizar el nombre haría que un experimento sobrescribiera los datos del anterior.
>
> Por eso se rechaza con `409` un nombre ya usado en esa TN. **Recomendación:** usa nombres descriptivos y únicos por experimento (p. ej. `exp-latencia-01`, `exp-throughput-02`).

### `DELETE /executions/{execution_id}/tn`
- Borrado manual de la Trial Network (deleted + purged) cuando tú decidas.
- Baja el túnel WireGuard y ejecuta destroy + purge en TNLCM.
- Respuestas: `202` borrado lanzado; `404` no existe o no tiene TN; `409` hay un experimento en curso o el borrado ya se lanzó/completó.

### `GET /executions/{execution_id}`
- Devuelve estado resumido de ejecucion.

### `GET /executions/{execution_id}/detail`
- Devuelve detalle completo (incluye ids y artifacts).
- Añade `tn_state`: el estado que TNLCM reporta en ese momento para la TN (`created`, `activated`, `destroyed`...), consultado en vivo contra TNLCM y no persistido en el registro.
- Es best-effort: queda a `null` si la ejecución todavía no tiene TN, si TNLCM ya no la conoce o si no responde; el resto de la respuesta no se ve afectada.

### `GET /executions/{execution_id}/summary`
- Resumen legible para experimentadores: qué pasó en cada fase, cuánto tardó y dónde han quedado los resultados, sin vocabulario interno.
- Se construye en vivo, así que puede consultarse mientras la ejecución sigue en curso (los pasos pasan de `pending` a `running` y a `ok`).
- `?format=markdown` devuelve el mismo contenido como texto (el `summary.md` que se guarda en `artifacts/<execution_id>/`).
- Detalle completo del formato en [`docs/TELEMETRY.md`](docs/TELEMETRY.md).

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
    "testcase_paths": ["TC_ping.yml"],
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
    "testcase_paths": ["TC_ping.yml"],
    "ues_paths": []
  },
  "dataset": {
    "output": ["logs", "csv", "dashboard", "raw"]
  },
  "auto_start_elcm": true
}
```

### Formatos Soportados de `component`

El validador centralizado (`app/domain/component_contract.py`) acepta dos formatos:

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

### Rechazo de valores vacíos (`""`)

Antes de cualquier otra validación, `POST /executions` recorre **todo el body** y **rechaza con `400`** si encuentra cualquier string vacío (`""` o solo espacios) en **cualquier** campo, sección o elemento de lista. La ejecución **no arranca**: hay que reenviar un POST bien formado, **rellenando el valor** o **eliminando el campo**.

Solo se inspecciona lo que envía el cliente (no los defaults del servidor), y cada ruta se reporta como dot-path para localizarla al instante:

```json
{
  "detail": {
    "empty_fields": [
      "experiment.testcase_paths[0]",
      "infrastructure.component.base.grafana_password",
      "infrastructure.descriptor_path"
    ],
    "message": "Algunos campos llegaron vacíos (\"\"). Rellénalos con un valor o elimínalos del body y reenvía el POST."
  }
}
```

> Nota: esto cubre también el caso de un parámetro `required` (ej. `grafana_password`) enviado como `""`: se rechaza aquí como campo vacío, no llega a la validación de `required` del overlay.

### Campo `auto_start_elcm`

- `true` (defecto): Ejecuta automáticamente TNLCM + ELCM secuencial
- `false`: Solo ejecuta TNLCM, requiere llamar manualmente a `POST /executions/{execution_id}/elcm`

### Campo `dataset.output`

Formato(s) de entrega del dataset. Acepta un **único nombre** (`"logs"`) o una **lista** combinable (`["logs", "csv", "dashboard", "raw", "files"]`). Las respuestas se guardan en `artifacts/<execution_id>/result/<experimento>/`, una subcarpeta por experimento.

`dataset` es **por experimento**: aparece tanto en el body de `POST /executions` (define la salida del primer experimento auto-arrancado) como en el body de `POST /executions/{id}/elcm` (define la salida de ese experimento concreto). Como una misma TN puede lanzar varios experimentos con salidas distintas, cada uno escribe en su propia subcarpeta `result/<experimento>/`.

| Valor | Qué hace | Salida en `result/<experimento>/` |
|-------|----------|-----------------------------------|
| `logs` | Recolecta los logs del experimento ELCM | `logs.json` + `metadata.json` |
| `csv` | Inyecta un TestCase que genera un CSV; descarga y extrae el ZIP de resultados de ELCM | `csv_query_<id>.csv` |
| `dashboard` | Inyecta un TestCase de Grafana; ELCM crea el dashboard (uid `Run<id>`) y se entrega su URL | `dashboard.json` |
| `raw` | Consulta InfluxDB directamente (Flux, sin TestCase) volcando cada measurement | `raw_<measurement>.csv` |
| `files` | Descarga el ZIP de resultados y extrae TODOS los ficheros del experimento (sin TestCase): borra los `.log` y descomprime los ZIP internos | ficheros del experimento |

- `csv` y `dashboard` **inyectan** su TestCase en el experimento (upload + descriptor) para que ELCM lo ejecute.
- `raw` y `files` **no** inyectan. `raw` replica la interfaz east/west consultando InfluxDB con el token del report TNLCM (nunca se persiste); `files` es la entrega de `csv` sin generar nada: recoge tal cual los ficheros que el experimento ya produjo.
- Compatibilidad: el string suelto (`"output": "logs"`) se sigue aceptando y se normaliza a lista.

### Variables globales de `dataset`

Además de `output`, el bloque `dataset` acepta **variables propias del modo de salida** que se use. Todas son opcionales y se resuelven con esta precedencia:

**valor del body → valor derivado del despliegue → default del overlay**

| Variable | Modos | Valor derivado si no se indica |
|---|---|---|
| `measurement` | `csv`, `dashboard`, `raw` | El `Measurement` del TestCase de captura (`*_capture*`) del experimento |
| `influx_host` | `csv` | La IP de monitorización del report TNLCM de esta TN |
| `influx_port` | `csv` | `8086` (overlay) |
| `influx_bucket` | `csv`, `raw` | El bucket del report TNLCM, o `testing` |
| `panel_interval` | `dashboard` | `5s` (overlay) |

```json
"dataset": {
  "output": ["csv", "raw"],
  "measurement": "OPEN5GS_KPIS",
  "influx_bucket": "testing"
}
```

Una variable cuyo modo dueño **no** esté en `output` se rechaza con 422 (fail-fast): pedir `influx_host` con `"output": ["logs"]` casi siempre significa que se olvidó el modo `csv`, y aceptarlo en silencio produciría una entrega distinta de la esperada. En `raw`, indicar `measurement` acota el volcado a ese measurement en vez de exportarlos todos.

## TestCases y variables globales (UE)

### Variables globales: el fichero UE

Un fichero **UE** no es un TestCase: es una lista de acciones estilo V1 cuya **clave raíz es su nombre**, y aquí se usa con un único `Run.Publish` para definir las variables que consumen todos los TestCases del experimento.

`templates/ELCM/TestCase/UE_Variables_TEMPLATE.yml` es la plantilla: se copia, se renombra la clave raíz y **solo se rellenan los valores** (los nombres ya están fijados). Se referencia por nombre de fichero:

```json
"experiment": {
  "name": "exp-demo",
  "testcase_paths": ["TC_Demo_Variables.yml"],
  "ues_paths": ["UE_Variables_TEMPLATE.yml"]
}
```

Reglas del motor que la plantilla ya respeta y que hay que mantener al copiarla:

- **Sin `Name:` ni `Version:`** — el endpoint de subida de ELCM rechaza `Name` sin `Version: 2`, y un UE es formato V1.
- **`Order` obligatorio** en cada acción de primer nivel. Las variables van en `Order: 0` para publicarse antes que nada.
- **Un único espacio de nombres por ejecución**: lo publicado por el UE lo ve *cualquier* TestCase del experimento, sin aislamiento por fichero.
- **En `@[Clave:default]` el default no puede contener `:`** — el Expander hace `split(':')` sin límite y un `ValueError` ahí tumba la fase Run entera. Para IPs y URLs, usar `@[SutIp]` sin default.

Sintaxis de consumo: `@[SutIp]` (publicado), `@[Publish.SutIp]` (grupo explícito), `@[Params.X]` (bloque `Parameters` del descriptor), `@{ExecutionId}` / `@{TempFolder}` / `@{Application}` (valores fijos del motor).

### Mapa de bandas de `Order`

El `Order` es **global a todo el experimento**: las acciones de todos los UEs y TestCases se mezclan en una única lista ordenada por `Order`, y dos TestCases que usen el mismo número se entrelazan de forma arbitraria. Para que la batería sea componible, cada fichero tiene su banda:

| Banda | Uso |
|---|---|
| `0–9` | UE / variables globales (`Run.Publish`) |
| `10–99` | Captura bloqueante (`Run.PrometheusToInflux` + `Run.AddMilestone`) |
| `100–699` | TestCases funcionales |
| `700–799` | Reservado |
| `800–899` | Entrega del dataset (`Run.InfluxToCsv`, `Run.CompressFiles`) |
| `900–999` | Notificación / cierre |

`tests/contract/test_testcase_library_contract.py` verifica esto en CI: todo fichero nuevo de `templates/ELCM/TestCase/` debe declarar su banda en `ORDER_BANDS`.

### Batería de TestCases

| Fichero | Orders | Para qué sirve | Requiere infra |
|---|---|---|---|
| `UE_Variables_TEMPLATE.yml` | 0 | Plantilla de variables globales | No |
| `TC_V2_BASE_TEMPLATE.yml` | 10–30 | Esqueleto de TestCase V2 (`Sequence`, `Dashboard`, `KPIs`) del que partir para escribir uno nuevo | No |
| `TC_Demo_Variables.yml` | 100–106 | Demo del mecanismo UE → TestCase: las dos sintaxis de expansión, derivar variables, y qué pasa con una variable inexistente | No |
| `TC_Demo_Flow.yml` | 120–123 | `Flow.Sequence` / `Flow.Parallel` (`@{Branch}`) / `Flow.Repeat` (`@{Iter0}`, `@{Iter1}`) y el patrón captura + ventana de medida | No |
| `TC_Demo_Python.yml` | 140–150 | Cadena de `Run.Evaluate` con Python real: agregados, comprensiones de lista, condicional y formateo para derivar KPIs | No |
| `TC_Util_Inventory.yml` | 300–304 | Inventario del host de ejecución entregado como ZIP en `/results` | No |
| `TC_Util_Connectivity.yml` | 320–326 | Ping al SUT, publica pérdida y RTT medio, y fija el veredicto con `Run.UpgradeVerdict` | Sí |
| `TC_Util_RestApi.yml` | 340–343 | `Run.RestApi` con los parámetros reales (`Host`/`Port`/`Endpoint`) y veredicto por código HTTP | Sí |
| `TC_Check_PublishTasks.yml` | 500–510 | Verifica `Run.PublishFromFile`, `Run.PublishFromPreviousTaskLog` y `Run.UpgradeVerdict` con datos deterministas | No |
| `TC_Util_ExportCsv.yml` | 820–822 | Export manual de InfluxDB a CSV+ZIP con query propia (alternativa a `dataset.output: ["csv"]`) | Sí |

Los que tocan infraestructura **asumen el componente ya configurado** (los grandes, tipo UERANSIM u Open5GS, requieren su configuración previa) y **empiezan comprobándolo**, de modo que fallan con un mensaje claro en vez de producir un dataset vacío.

Dos trampas del motor que la batería documenta en sus cabeceras:

- **`Run.InfluxToCsv` y `Run.CliExecute` no registran nada en `GeneratedFiles`.** Sin un `Run.CompressFiles` posterior, el fichero se genera, se ve en el log y se pierde: no llega a `GET /execution/{id}/results`.
- **Las variables de flujo no se heredan en flujos anidados.** `@{Branch}` solo se sustituye en los hijos *directos* de `Flow.Parallel`, y `@{Iter0}`/`@{Iter1}` en los de `Flow.Repeat`.

## Flujo de uso recomendado

### Ciclo de vida de la TN y estados

La Trial Network se queda **viva por defecto** para aceptar varios experimentos. Comportamiento según los flags del `DatasetDescriptor`:

- `auto_start_elcm=false` → despliega y se queda en `TN_READY`. No borra nada; espera llamadas manuales a `/elcm`. (`ephemeral_tn` se ignora.)
- `auto_start_elcm=true` + `ephemeral_tn=false` (habitual) → despliega, lanza el 1er experimento y vuelve a `TN_READY`. Acepta más `/elcm` o el borrado manual.
- `auto_start_elcm=true` + `ephemeral_tn=true` (un solo uso) → despliega, lanza el 1er experimento y borra la TN automáticamente.

Estados: `PENDING → VALIDATING → DEPLOYING → TN_READY ⇄ RUNNING_EXPERIMENT / COLLECTING → DESTROYING → DESTROYED` (o `FAILED`). El borrado manual se dispara con `DELETE /executions/{id}/tn`.

### Opción 1: Automático (Recomendado)

Con VPN automática ya solucionada, la forma más sencilla es:

```
1. POST /login  (opcional pero recomendado)
2. POST /executions con auto_start_elcm=true (defecto)
3. GET /executions/{execution_id} para monitorear
4. ✓ TNLCM + 1er experimento ELCM; la TN queda viva en TN_READY (salvo ephemeral_tn=true)
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
3. GET /executions/{execution_id} hasta TN_READY
4. POST /executions/{execution_id}/elcm con {"experiment": {...}, "dataset": {...}}  (repetible, nombre único, salida por experimento)
5. GET /executions/{execution_id} hasta que vuelva a TN_READY
6. DELETE /executions/{execution_id}/tn  (borrado manual cuando termines)
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
5. ELCM: genera el Experiment Descriptor → upload testcases (+ TestCases de dataset csv/dashboard) → run experiment → collect dataset (logs/csv/dashboard/raw) en `result/<experimento>/`
6. Al terminar el experimento la TN vuelve a `TN_READY` (sigue viva y con el túnel WireGuard activo): puedes lanzar más `POST /executions/{id}/elcm`.
7. Si `ephemeral_tn=true`, tras ese primer experimento se baja el túnel WireGuard y se destruye la TN (`DESTROYED`).
8. Borrado manual en cualquier momento con `DELETE /executions/{id}/tn`.

## Arquitectura de Componentes y Validación

### Extractor Centralizado (`app/domain/component_contract.py`)

El módulo `extract_component_template_values()` centraliza la lógica de:
1. **Normalización**: Convierte formato plano → estrutura sección/campo
2. **Validación**: Verifica contra campos editables del overlay TNLCM
3. **Reutilización**: Se usa en `app/api/validation.py` (validación del payload) y `app/rendering/` (extracción durante render)

**Consumidores:**

| Módulo | Uso |
|---|---|
| `app/api/validation.py` | `validate_components_or_raise()`, invocada por `POST /executions` |
| `app/rendering/` | Extracción de campos editables antes de render `ytt` en `generate_tnlcm_descriptor()` |

**Ventajas de centralización:**
- Evita duplicación de lógica validación ↔ generación
- Garantiza comportamiento consistente en ambos puntos
- Facilita mantenimiento y pruebas

### Overlays TNLCM

Cada template TNLCM tiene un overlay que define qué campos pueden ser editados por el payload:

- **Ubicación**: `templates/TNLCM/overlays/<template_name>.yaml`
- **Estructura**: Define secciones y campos editables (ej. `monitoring: [influxdb_user, influxdb_password, ...]`)
- **Carga**: Automática mediante `overlay_editable_fields_for_template()` en `app/rendering/overlays.py`

## Automatizacion WireGuard

- Implementacion en `app/adapters/wireguard.py`.
- Helper de sistema en `app/adapters/wireguard_helper.py`.
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
| 2026-08 | `orchestrator.py` (1.632 L) descompuesto en `state.py`, `phases/{tnlcm,elcm,teardown,results}.py`, `errors.py`, `background.py` y `reporting.py`; el coordinador queda en 187 L |
| 2026-08 | `main.py` (802 L) dividido en `app/api/`: 5 routers mas deps/phases/validation/errors; `main.py` queda en 67 L de composicion |
| 2026-08 | Reorganizacion de `app/` por capas: `core/`, `domain/`, `services/`, `adapters/`, `rendering/`, `storage/`, `observability/`; desaparecen `app/utils/` y `app/generators/` |
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
| 2026-05-17 | Convertidos templates TNLCM base a `ytt` (`@ytt:data` / `@data.values`) y añadida documentación sobre qué valores debe contener el `DatasetDescriptor` |
| **2026-05-30** | **Extractor Centralizado**: Módulo `app/domain/component_contract.py` con `extract_component_template_values()` para normalizar y validar campos `component.<template>.<field>` contra editables del overlay; soporta formatos plano y anidado con retrocompatibilidad |
| 2026-07 | **Ciclo de vida persistente**: la TN queda viva (`TN_READY`) tras cada experimento; nuevos estados y `DELETE /executions/{id}/tn` para borrado manual; `ephemeral_tn` para TN de un solo uso |
| 2026-07 | **Health desdoblado**: `/health/services` (liveness orquestador + TNLCM) y `/health/components` (InfluxDB/Grafana/Prometheus/ELCM) |
| 2026-07 | **`dataset.output` multi-formato**: acepta lista o string de `logs`/`csv`/`dashboard`/`raw`; las respuestas se guardan en `artifacts/<execution_id>/result/` |
| 2026-07 | **csv/dashboard**: nuevo generador ELCM (`app/generators/elcm_dataset.py`) que renderiza con `ytt` un TestCase de dataset y lo inyecta en el experimento; descarga y extracción del ZIP de resultados (`app/utils/results_bundle.py`) |
| 2026-07 | **raw**: consulta directa a InfluxDB v2 (Flux) replicando la interfaz east/west (`app/utils/influx_raw.py`), un CSV por measurement |
| 2026-07 | `app/generators.py` dividido en el paquete `app/generators/` (`tnlcm_overlay`, `tnlcm_renderer`, `elcm_dataset`) |
| 2026-07 | **`dataset` por experimento**: `POST /executions/{id}/elcm` admite su propio `dataset.output`; cada experimento escribe en `result/<experimento>/`. El **Experiment Descriptor** se genera por ejecución (JSON, sin `ytt`) desde las UEs/TestCases del `experiment` y se guarda junto al TN Descriptor; se elimina el fallback a `examples/` |
| 2026-08 | **Variables globales vía UE**: `ues_paths` funciona de verdad (los UEs se suben con `file_type="ues"` y el descriptor los referencia por su nombre interno, no por la ruta); nueva plantilla `examples/UE_Variables_TEMPLATE.yml` con `Run.Publish` y nombres fijados |
| 2026-08 | **Batería de TestCases** en `examples/` (demo, utilidad y verificación de `PublishFromFile`/`PublishFromPreviousTaskLog`/`UpgradeVerdict`) con **bandas de `Order` disjuntas** para que sean componibles; contrato verificado en CI (`tests/test_examples_contract.py`) |
| 2026-08 | **Variables globales de `dataset`** (`measurement`, `influx_host`/`influx_port`/`influx_bucket`, `panel_interval`) validadas por modo de salida; el TestCase CSV deja de llevar measurement/bucket/IP hardcodeados y pasa a la banda de entrega (`Order` 800/801) |
| 2026-07 | **TestCases verbatim + fix de comillas**: los TestCases del body se suben **tal cual** desde `examples/` (ya no se re-renderizan: eso corrompía comillas/indentación) y el descriptor los referencia por su `Name:` interno. Los TestCases de dataset (csv/dashboard) se re-serializan forzando comillas dobles para que `ytt` no rompa el entrecomillado (queries de Prometheus, `@{ExecutionId}`) |
| 2026-08 | **Biblioteca de TestCases en `templates/ELCM/TestCase/`**: `testcase_paths`/`ues_paths` dejan de resolver contra `examples/` (`EXAMPLES_DIR`) y lo hacen contra la biblioteca de plantillas, que cuelga de la raíz del repositorio y no de `cwd`. La búsqueda es **por nombre de fichero** sobre un directorio plano, así que un `../` en la referencia no saca la resolución de ahí. El contrato de CI pasa a `tests/contract/test_testcase_library_contract.py`; `EXAMPLES_DIR` queda solo para `infrastructure.descriptor_path` |

## Uso con Postman

- Coleccion: `API_JSON/DaaS.postman_collection.json`
- Variables: `baseUrl`, `apiKey`, `executionId`

## Debugging y Tests

Ejecutar suite completa de tests (132 tests):

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
- **`docs/INCIDENCIA_TNLCM_ACTIVATE_500.md`**: Informe del fallo en el que `activate` devuelve 500 mientras el despliegue sigue vivo en Jenkins, y la colisión despliegue/destrucción que deja el `tn_id` inutilizable
- **`Recursos/300526_Resumen.md`**: Documentación técnica detallada archivo por archivo (SI EXISTE)

### Documentación Legacy (NO Usar)

Los siguientes archivos contienen snapshots históricos de fases anteriores del desarrollo. **NO se recomiendan para nuevas implementaciones**:

- `docs/UNIFIED_EXECUTIONS_API.md` - Documentación de fase de unificación (desalineada con estado actual)
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

Si necesitas validar el flujo completo, usa primero la coleccion Postman y revisa estos dos endpoints para inspeccionar qué ha pasado:

- `GET /executions/{execution_id}/summary` - qué pasó en cada fase, cuánto tardó y qué falló, en lenguaje natural
- `GET /executions/{execution_id}/detail` - registro completo con artifacts, el error en crudo y el `tn_state` que TNLCM reporta ahora mismo para la TN

Para problemas de validación de componentes, consulta la sección "Validación de Componentes" arriba y revisa los overlays TNLCM en `templates/TNLCM/overlays/`.

Para entender en detalle la arquitectura del extractor de componentes, ver `app/domain/component_contract.py` y sus consumidores en `app/main.py` y `app/rendering/`.

