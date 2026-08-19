"""Cliente HTTP fino para la API del DaaS Orchestrator.

Centraliza las llamadas a los endpoints existentes para que sus consumidores no
repitan la construcción de peticiones ni el manejo de errores. Cada método
devuelve el cuerpo JSON ya deserializado en caso de éxito, o levanta `ApiError`
con un mensaje legible (y el `detail` original disponible para pintarlo).

Lo usan DOS clientes y por eso vive en `app/` y no bajo `ui/`: la interfaz web
(`ui/streamlit_app.py`) y el CLI (`app/cli.py`). La UI es una dependencia
opcional que no existe en la rama principal, así que colgar de ella el único
cliente ataba la mainline a Streamlit; al revés no pasa nada, porque este módulo
no importa ni Streamlit ni FastAPI.

El descriptor sale de aquí SIEMPRE como **fichero YAML subido**
(`multipart/form-data`, campo `descriptor`). La API acepta además el cuerpo en
`application/yaml` y en JSON, y las colecciones Postman las ejercitan, pero los
dos consumidores ofrecen una sola vía a propósito: teniendo tres, lo único que
pasaba era mezclarlas. Ver `docs/UI_YAML_MIGRATION.md`.

Es un cliente SÍNCRONO a propósito: ninguno de los dos consumidores tiene event
loop —Streamlit ejecuta el script en su propio hilo y el CLI es un proceso de un
solo uso—, así que usar `httpx` en modo bloqueante es lo correcto aquí.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

# Donde escucha el orquestador cuando nadie dice lo contrario. Lo comparten la UI
# (panel lateral) y el CLI (--base-url), para que el valor por defecto sea uno.
DEFAULT_BASE_URL = "http://localhost:8000"

# Timeout por defecto para operaciones ligeras (estado, health, login).
DEFAULT_TIMEOUT_SECONDS = 30.0

# Los GET que se sondean en bucle (estado y resumen, cada 5 s desde un fragment)
# van con un tope corto a proposito: son llamadas BLOQUEANTES en el hilo del
# script, asi que con la API atascada un tope largo apila reruns y congela la
# sesion, que es justo lo que se quiere evitar mientras se espera un despliegue.
POLL_TIMEOUT_SECONDS = 10.0

# Topes de espera de las fases del ciclo de vida, copiados de
# `app/services/orchestrator.py`. Los tres endpoints bloquean por defecto
# (`wait=true`) y responden 504 al agotar SU tope, sin cortar el trabajo.
#
# El cliente tiene que aguantar MAS que el servidor. Con timeouts por debajo
# —eran 120 s y 30 s— la UI abortaba la conexion a los dos minutos y enseñaba
# «Timeout al contactar» mientras el despliegue seguia invisible por detras: el
# bloqueo se veia en Postman y no en la UI por esto, no porque la API no
# bloquease. El margen es para que el 504 llegue a leerse, que es lo unico que
# distingue «sigue en curso» de «se cayo el servicio».
_PHASE_MARGIN_SECONDS = 120.0
CREATE_TIMEOUT_SECONDS = 2400.0 + _PHASE_MARGIN_SECONDS  # TNLCM: 40 min
ELCM_TIMEOUT_SECONDS = 4200.0 + _PHASE_MARGIN_SECONDS  # experimento + dataset: 70 min
TEARDOWN_TIMEOUT_SECONDS = 3000.0 + _PHASE_MARGIN_SECONDS  # borrado: 50 min


def _phase_timeout(read_seconds: float) -> httpx.Timeout:
    """Timeout de una fase: mucho para leer, poco para conectar.

    Separar `connect` del `read` es lo que permite distinguir los dos fallos que
    de otro modo se confunden en una espera de 70 minutos: si el servicio no
    esta, falla en segundos; si esta trabajando, se espera lo que haga falta.
    """
    return httpx.Timeout(read_seconds, connect=10.0, write=30.0, pool=10.0)


# Formatos de entrega admitidos por `dataset.output` (app/domain/descriptor.py:
# DatasetOutput). 'files' recolecta los ficheros que el TestCase deja publicados.
DATASET_OUTPUTS: tuple[str, ...] = ("logs", "csv", "dashboard", "raw", "files")

# Variables globales del bloque `dataset` y el modo al que pertenece cada una
# (espejo de DATASET_MODE_VARIABLES). El servidor responde 422 si se declara una
# variable cuyo modo no esta en `output`; tenerlo aqui permite avisar antes de
# enviar, con el nombre del modo que falta.
DATASET_MODE_VARIABLES: dict[str, tuple[str, ...]] = {
    "measurement": ("csv", "dashboard", "raw"),
    "influx_host": ("csv",),
    "influx_port": ("csv",),
    "influx_bucket": ("csv", "raw"),
    "panel_interval": ("dashboard",),
}


def variables_for_outputs(outputs: Iterable[str]) -> tuple[str, ...]:
    """Variables globales de `dataset` que el servidor acepta con estos `output`.

    Es la misma regla que aplica `_reject_variables_of_inactive_modes` con un
    422: una variable solo vale si alguno de sus modos duenos esta pedido. El
    orden es el de `DATASET_MODE_VARIABLES` para que el formulario no reordene
    los campos al cambiar de modo.
    """
    active = set(outputs)
    return tuple(
        name for name, owners in DATASET_MODE_VARIABLES.items() if active.intersection(owners)
    )


# Content-Type del fichero que se adjunta. La API admite tres codificaciones
# (body JSON, body YAML y fichero subido en multipart); la UI usa solo la
# ultima, ver `docs/UI_YAML_MIGRATION.md`.
YAML_MEDIA_TYPE = "application/yaml"

# Nombre del campo del formulario multipart que transporta el fichero, fijado por
# `app/api/body_formats.py:DESCRIPTOR_FIELD`.
DESCRIPTOR_FIELD = "descriptor"

# Tope del cuerpo que aplica el servidor (`MAX_BODY_BYTES`). Se replica para
# poder avisar en el cliente en vez de gastar una subida que acabara en 413.
MAX_DESCRIPTOR_BYTES = 1024 * 1024


class ApiError(Exception):
    """Error de comunicación con la API ya traducido a mensaje legible.

    Attributes:
        message: Texto listo para mostrar al usuario.
        status_code: Código HTTP devuelto (None si ni siquiera hubo respuesta).
        detail: Cuerpo `detail` original (str o dict), útil para listar campos.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


def _extract_detail(response: httpx.Response) -> Any:
    """Devuelve el campo `detail` de una respuesta de error de FastAPI.

    FastAPI encapsula los errores en `{"detail": ...}`, donde `detail` puede ser
    un string o un dict (p. ej. `{"empty_fields": [...]}`). Si el cuerpo no es
    JSON, se devuelve el texto crudo.
    """
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()
    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    return payload


def yaml_error_position(detail: Any) -> tuple[int, int] | None:
    """(línea, columna) del error de sintaxis YAML, si el servidor las envía.

    Solo el camino YAML las trae: PyYAML las saca del `problem_mark` y la API las
    reexpide contando desde 1, como los editores. Es lo que permite señalar el
    punto exacto en el editor de la UI, algo que el camino JSON nunca dio.
    """
    if not isinstance(detail, dict) or "yaml_error" not in detail:
        return None
    line, column = detail.get("line"), detail.get("column")
    if isinstance(line, int) and isinstance(column, int):
        return line, column
    return None


def _format_error(status_code: int, detail: Any) -> str:
    """Traduce (código, detail) a un mensaje corto y legible para la UI."""
    if isinstance(detail, dict):
        if "yaml_error" in detail:
            # Sintaxis YAML rota. El `message` explica qué hacer; la posición, dónde.
            message = str(detail.get("message") or "El descriptor no es YAML válido.")
            position = yaml_error_position(detail)
            where = f" (línea {position[0]}, columna {position[1]})" if position else ""
            return f"{message}{where} — {detail['yaml_error']}"
        if "missing_field" in detail:
            return (
                f"El fichero no viajó en el campo '{detail['missing_field']}' de la "
                "petición multipart."
            )
        if "received_bytes" in detail and "max_bytes" in detail:
            received = int(detail["received_bytes"]) / 1024
            maximum = int(detail["max_bytes"]) / 1024
            return f"El descriptor ocupa {received:.1f} KiB y el máximo son " f"{maximum:.1f} KiB."
        if "empty_fields" in detail:
            fields = ", ".join(detail.get("empty_fields", []))
            return f"Campos vacíos que debes rellenar o quitar: {fields}"
        if "invalid_fields" in detail:
            fields = ", ".join(detail.get("invalid_fields", []))
            return f"Campos inválidos: {fields}"
        message = detail.get("message") or detail.get("detail")
        if message:
            return str(message)
        return f"HTTP {status_code}: {detail}"

    if isinstance(detail, list):
        # 422 de Pydantic: una entrada por campo mal formado. `loc` viene como
        # ["body", "dataset", "output", 0]; se pinta como ruta de puntos y sin el
        # "body" inicial, que no dice nada a quien escribe el descriptor.
        problems = []
        for item in detail:
            if not isinstance(item, dict):
                problems.append(str(item))
                continue
            parts = [str(part) for part in item.get("loc", []) if part != "body"]
            where = ".".join(parts) or "descriptor"
            problems.append(f"{where}: {item.get('msg', 'valor inválido')}")
        if problems:
            return "El descriptor no encaja con el esquema — " + "; ".join(problems)

    text = str(detail).strip() if detail else ""
    if status_code == 401:
        return "API key inválida: revisa la clave configurada."
    if status_code == 413:
        return text or "El descriptor supera el tamaño máximo admitido (1 MiB)."
    if status_code == 404:
        return text or "Recurso no encontrado."
    if status_code == 409:
        return text or "Conflicto: hay una operación en curso o ya realizada."
    if status_code == 504:
        return text or "Timeout contactando con un servicio dependiente."
    return text or f"Error HTTP {status_code}."


def _parse_response(response: httpx.Response, *, raw: bool = False) -> Any:
    """Devuelve el JSON en caso de éxito; levanta `ApiError` en caso contrario.

    Con `raw=True` devuelve los bytes sin tocar, que es lo que necesita la
    descarga del ZIP: intentar deserializarlo lo corromperia.
    """
    if response.is_success:
        if raw:
            return response.content
        try:
            return response.json()
        except ValueError:
            return (response.text or "").strip()

    detail = _extract_detail(response)
    raise ApiError(
        _format_error(response.status_code, detail),
        status_code=response.status_code,
        detail=detail,
    )


@dataclass(frozen=True)
class PhaseResult:
    """Desenlace de una fase: el codigo HTTP y el cuerpo.

    `app/api/phases.py` lo dice explicito: «el codigo HTTP es la respuesta». Un
    200 y un 207 traen el mismo cuerpo y significan cosas distintas —207 es «la
    TN esta pero el tunel hay que montarlo a mano» o «el experimento acabo pero
    el dataset quedo a medias»—, asi que devolver solo el JSON perdia justo la
    mitad util.
    """

    status_code: int
    payload: Any


@dataclass(frozen=True)
class Descriptor:
    """Descriptor a subir como fichero: el nombre importa, viaja en el multipart."""

    filename: str
    content: bytes


def _reject_oversized(size: int) -> None:
    """Corta en el cliente lo que el servidor rechazaria con un 413.

    Comprobarlo aqui ahorra subir un fichero que ya se sabe que no cabe, y da el
    mismo mensaje que daria la API.
    """
    if size > MAX_DESCRIPTOR_BYTES:
        raise ApiError(
            f"El descriptor ocupa {size / 1024:.1f} KiB y el maximo son "
            f"{MAX_DESCRIPTOR_BYTES / 1024:.1f} KiB.",
            status_code=413,
            detail={"received_bytes": size, "max_bytes": MAX_DESCRIPTOR_BYTES},
        )


@dataclass
class ApiClient:
    """Wrapper de una sola instancia de conexión (base URL + API key)."""

    base_url: str
    api_key: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def _headers(self, *, auth: bool) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if auth:
            headers["X-API-Key"] = self.api_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        json: Any = None,
        files: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
        raw: bool = False,
        phase: bool = False,
    ) -> Any:
        """Ejecuta la petición y normaliza errores de red y de estado HTTP.

        `files` es lo que distingue la subida del descriptor del resto de
        llamadas: en multipart el Content-Type lo pone httpx, porque tiene que
        incluir el `boundary`.

        Se abre un `httpx.Client` explícito en vez de llamar a la función de
        módulo `httpx.request(...)`. No es cosmético: `httpx.request` resuelve la
        clase `Client` desde los globals de `httpx._api`, así que sustituir
        `httpx.Client` —que es como las pruebas enchufan un `MockTransport`— NO
        la afecta y las peticiones se escapan a la red real sin avisar.
        """
        url = f"{self.base_url.rstrip('/')}{path}"
        try:
            with httpx.Client(timeout=timeout or self.timeout) as http_client:
                response = http_client.request(
                    method,
                    url,
                    headers=self._headers(auth=auth),
                    json=json,
                    files=files,
                    params=params,
                )
        except httpx.TimeoutException as exc:
            if phase:
                raise ApiError(
                    "Se agotó la espera del cliente, pero la operación puede seguir "
                    "en curso: compruébalo en el resumen de la ejecución.",
                    status_code=None,
                ) from exc
            raise ApiError(f"Timeout al contactar {url}") from exc
        except httpx.HTTPError as exc:
            # Una fase puede durar 70 min con el socket en silencio, asi que un
            # corte de conexion (proxy, NAT, suspension del portatil) es
            # esperable y NO significa que la operacion haya fallado: el
            # servidor sigue a lo suyo. Decir «no se pudo conectar» aqui seria
            # mentir sobre el desenlace.
            if phase:
                raise ApiError(
                    "Se perdió la conexión con el servidor, pero la operación "
                    "puede seguir en curso: compruébalo en el resumen de la ejecución.",
                    status_code=None,
                    detail=str(exc),
                ) from exc
            raise ApiError(f"No se pudo conectar con {url}: {exc}") from exc

        payload = _parse_response(response, raw=raw)
        return PhaseResult(response.status_code, payload) if phase else payload

    def _post_descriptor(
        self,
        path: str,
        descriptor: Descriptor,
        timeout: float,
        *,
        wait: bool = True,
    ) -> Any:
        """POST del descriptor como fichero subido (`multipart/form-data`).

        Es la unica via que usan la UI y el CLI. La API tambien acepta el
        descriptor como cuerpo `application/yaml` o JSON —y las colecciones
        Postman lo ejercitan— pero ofrecer varias desde la interfaz solo
        invitaba a mezclarlas.

        Con `wait=True` el `timeout` es el de la FASE, no el de la red: estos
        endpoints bloquean hasta que la fase termina. Con `wait=False` el
        servidor responde 202 al instante, asi que se usa el tope corto: esperar
        el tope de fase por una respuesta inmediata solo sirve para tapar una
        caida del servicio durante cuarenta minutos.
        """
        _reject_oversized(len(descriptor.content))
        return self._request(
            "POST",
            path,
            auth=True,
            files={DESCRIPTOR_FIELD: (descriptor.filename, descriptor.content, YAML_MEDIA_TYPE)},
            params={"wait": str(wait).lower()},
            timeout=_phase_timeout(timeout) if wait else DEFAULT_TIMEOUT_SECONDS,
            phase=True,
        )

    # ----- Health -----

    def health_services(self) -> Any:
        """Liveness del orquestador y de TNLCM (no requiere API key)."""
        return self._request("GET", "/health/services", auth=False)

    def health_components(self) -> Any:
        """Salud HTTP de InfluxDB/Grafana/Prometheus/ELCM (requiere API key)."""
        return self._request("GET", "/health/components", auth=True)

    # ----- Auth TNLCM -----

    def login_tnlcm(self) -> Any:
        """Refresca el token TNLCM usando las credenciales del .env del servidor."""
        return self._request("POST", "/login", auth=True)

    def register_tnlcm(
        self,
        username: str,
        password: str,
        email: str | None = None,
        org: str | None = None,
    ) -> Any:
        """Registra un usuario en TNLCM (los datos van por query string)."""
        params: dict[str, Any] = {"username": username, "password": password}
        if email:
            params["email"] = email
        if org:
            params["org"] = org
        return self._request("POST", "/register", auth=False, params=params)

    # ----- Ejecuciones -----

    def create_execution(self, descriptor: Descriptor, *, wait: bool = True) -> Any:
        """Lanza una ejecución y espera a que la VPN quede resuelta.

        El endpoint bloquea (`wait=true` por defecto): no responde hasta que se
        puede llamar a /elcm. El código HTTP dice cómo fue — 200 túnel arriba,
        207 hay que montarlo a mano, 502 falló el despliegue, 504 se agotó el
        tope del servidor y el despliegue continúa por detrás.

        Con `wait=False` la respuesta es un 202 inmediato y el despliegue sigue
        por detrás: hay que sondear con `get_execution`.
        """
        return self._post_descriptor("/executions", descriptor, CREATE_TIMEOUT_SECONDS, wait=wait)

    def get_execution(self, execution_id: str) -> Any:
        """Estado resumido de una ejecución."""
        return self._request(
            "GET", f"/executions/{execution_id}", auth=True, timeout=POLL_TIMEOUT_SECONDS
        )

    def get_execution_detail(self, execution_id: str) -> Any:
        """Registro completo de una ejecución (artifacts, experimentos, error)."""
        return self._request(
            "GET", f"/executions/{execution_id}/detail", auth=True, timeout=POLL_TIMEOUT_SECONDS
        )

    def get_execution_summary(self, execution_id: str, *, as_markdown: bool = False) -> Any:
        """Resumen legible: pasos, duraciones, resultados y errores explicados.

        El backend lo construye en vivo, así que puede consultarse mientras la
        ejecución sigue en curso. Con `as_markdown=True` devuelve el texto del
        informe (el mismo `summary.md` que se guarda en `artifacts/`) en lugar
        del JSON; `_parse_response` ya se encarga de devolverlo como str.
        """
        params = {"format": "markdown"} if as_markdown else None
        return self._request(
            "GET",
            f"/executions/{execution_id}/summary",
            auth=True,
            params=params,
            timeout=POLL_TIMEOUT_SECONDS,
        )

    def start_elcm(self, execution_id: str, descriptor: Descriptor, *, wait: bool = True) -> Any:
        """Lanza un experimento ELCM y espera a que el dataset esté recolectado.

        El cuerpo lleva solo `experiment` y `dataset`; la infraestructura ya
        existe y no se vuelve a describir. Bloquea hasta que el dataset está en
        disco, no solo hasta que el experimento para; con `wait=False` responde
        202 al instante.
        """
        return self._post_descriptor(
            f"/executions/{execution_id}/elcm", descriptor, ELCM_TIMEOUT_SECONDS, wait=wait
        )

    def delete_tn(self, execution_id: str) -> Any:
        """Borra la Trial Network y espera a que quede purgada."""
        return self._request(
            "DELETE",
            f"/executions/{execution_id}/tn",
            auth=True,
            timeout=_phase_timeout(TEARDOWN_TIMEOUT_SECONDS),
            phase=True,
        )

    def download_execution(self, execution_id: str, *, secrets: bool = False) -> bytes:
        """Descarga el ZIP con todo lo que ha dejado la ejecución.

        Con `secrets=False` (por defecto) el servidor deja fuera los ficheros con
        claves de acceso: la config de WireGuard y los informes crudos de TNLCM.
        """
        return self._request(
            "GET",
            f"/executions/{execution_id}/download",
            auth=True,
            params={"secrets": str(secrets).lower()},
            raw=True,
        )
