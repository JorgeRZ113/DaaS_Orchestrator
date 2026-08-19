# UI web (Streamlit) — DaaS Orchestrator

Interfaz de navegador para operar el orquestador sin Postman. Es una app
**companion** independiente: habla con la API (FastAPI) por HTTP y no comparte
proceso con el servicio. Lo unico que importa de `app/` es `app/client.py`, el
cliente HTTP que comparte con el CLI (`daas`); no toca la logica interna.

Hace dos cosas. Sustituye a Postman en el día a día (lanzar, consultar, borrar)
y, sobre todo, **produce el Dataset Descriptor**: lo que sale del formulario no
es una petición, es el fichero YAML que el anteproyecto promete, y se puede
descargar, versionar en git y reenviar después sin la UI delante, desde consola
o CI. Por eso las pestañas de ejecución y de experimento giran alrededor de un
editor de YAML. Ver `docs/UI_YAML_MIGRATION.md`.

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
que vive en `app/observability/execution_summary.py`. Ver `docs/TELEMETRY.md`.

## El descriptor en YAML

Las pestañas *Nueva ejecución* y *Experimento ELCM* comparten la misma
estructura: tres fuentes intercambiables que alimentan un editor, y desde el
editor se descarga o se envía.

| Fuente | Para qué |
|---|---|
| **Formulario** | Genera el YAML desde cero; es el camino recomendado la primera vez |
| **Fichero** | Sube un `.yaml`/`.yml` que ya tengas versionado |
| **Ejemplo** | Carga uno de los 5 descriptores comentados de `examples/descriptors/` |

El editor comprueba la sintaxis mientras escribes y **Descargar** guarda el
fichero tal cual se enviaría. El volcado usa la misma configuración que el
servidor al persistirlo (`app/storage/artifacts.py:_dump_yaml`), así que el
descriptor que descargas y el que queda en `artifacts/` son el mismo texto.

El descriptor viaja **siempre como fichero subido** (`multipart/form-data`,
campo `descriptor`). Si el servidor rechaza la sintaxis, la UI marca la línea
exacta en un fragmento del propio descriptor — es lo que aporta el formato YAML
y que el JSON nunca dio.

> La API acepta tres codificaciones del mismo cuerpo (JSON, `application/yaml` y
> fichero subido) y las colecciones Postman las ejercitan. **La interfaz ofrece
> una sola** a propósito: teniendo varias lo único que pasaba era mezclarlas, y
> el fichero es el modo que encaja con lo que la UI produce.

## El formulario

Genera el descriptor y reacciona a lo que elijas: no pinta campos que no vayan a
servir. Concretamente:

- **`dataset.output`** decide qué variables globales aparecen. Con `logs` no hay
  ninguna; con `csv` salen `measurement`, `influx_host`, `influx_port` e
  `influx_bucket`; con `dashboard`, `measurement` y `panel_interval`. Es la misma
  regla que aplica el servidor, que responde 422 si declaras una variable cuyo
  modo no has pedido.
- **Los componentes** se eligen con botones (los 11 a la vista) y cada uno abre
  su propio bloque con **sus** campos, leídos del catálogo
  `examples/descriptors/REFERENCIA_componentes.yaml`.

### Campos de un componente

Los **obligatorios** se pintan siempre y llevan `*` en la etiqueta. Los demás
pasan por un selector **Campos a personalizar**, porque hay componentes con
muchos (`ueransim_split` tiene 39) y llenar la página de cajas vacías no ayuda.

Elegir un componente **no obliga a tocar nada**: lo que no rellenes se queda con
el valor por defecto de su overlay. La excepción son los obligatorios — y son de
verdad obligatorios: `base` y `mongodb` son los únicos que los tienen, y
nombrarlos sin ellos no es «usa los defaults», es un 400 del servidor.

Los campos que viven en dos secciones del mismo componente (`name` en
`int_p4_sw`, los `vnet_*` de `ocf`) se muestran como `sección.campo` y se emiten
anidados. No es cosmético: en formato plano el backend no los rechaza, los
asigna en silencio a una sola de las secciones y la otra queda inalcanzable.

## Esperas largas y candado

Los tres endpoints del ciclo de vida **bloquean**: no responden hasta que la fase
termina — la VPN resuelta en `/executions`, el dataset recolectado en `/elcm`, la
TN purgada en el borrado. El desenlace viene en el código HTTP (200 completo,
207 incompleto, 502 falló, 504 sigue en curso), así que no hay que ir a sondear
nada. Los topes del servidor son 40, 70 y 50 minutos.

La UI lanza esas peticiones **en segundo plano** para no congelarse: una llamada
síncrona bloquearía la sesión entera de Streamlit, y con ella las pestañas de
Estado y Resumen, que es justo lo que interesa mirar durante la espera. Mientras
dura verás un panel de progreso arriba, visible desde cualquier pestaña.

| Acción | Con una operación en curso |
|---|---|
| Lanzar ejecución / experimento, borrar TN | 🔒 bloqueadas, para que no choquen |
| Estado, Resumen, descargar el ZIP | ✅ siguen disponibles |

El candado es **por sesión**: dos pestañas del navegador son dos sesiones y
podrían lanzar a la vez. Quien lo impide de verdad es el servidor, que responde
409 si ya hay un despliegue en curso.

> Si cierras la pestaña más de dos minutos, la sesión de Streamlit caduca y la UI
> pierde de vista el trabajo. **El servidor no**: la operación sigue y se ve en
> la pestaña Resumen.

## Descargar la ejecución

Desde *Estado*, **Preparar ZIP** empaqueta todo lo que ha dejado la ejecución:
el descriptor que la produjo, el resumen, los datasets recolectados, los
artefactos intermedios y un `README.md` generado que explica qué es cada cosa.

Por defecto **no** incluye los ficheros con claves de acceso —la configuración de
WireGuard y los informes crudos de TNLCM, que llevan la clave privada del túnel y
el token de InfluxDB—. La casilla los añade si los necesitas; el ZIP deja
entonces de ser compartible sin revisarlo.

## Notas

- No requiere CORS: las peticiones salen del servidor de Streamlit, no del
  navegador.
- Los campos que dejes vacíos se omiten del descriptor (el backend rechaza
  strings vacíos), así evitas el error 400 de `empty_fields`.
- El catálogo de campos sale de `examples/descriptors/REFERENCIA_componentes.yaml`,
  que una prueba mantiene sincronizado con `templates/TNLCM/overlays/`. Si añades
  un campo a un overlay y no lo reflejas ahí, la UI no lo ofrecerá.
- Los `testcase_paths` y `ues_paths` se resuelven por **nombre de fichero**
  contra `templates/ELCM/TestCase/`; no admiten rutas.
- El descriptor no puede pasar de 1 MiB: la UI lo comprueba antes de subirlo.
