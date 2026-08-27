"""Fixtures compartidas por toda la suite.

pytest carga este fichero automaticamente para cualquier prueba bajo `tests/`,
asi que es el unico sitio donde debe vivir el andamiaje comun. Antes de que
existiera, cada modulo nuevo se traia su propia copia: `isolate_artifacts_dir`
llego a estar replicada literalmente en 6 ficheros y `isolate_orchestrator_state`
en 4, con el mismo MD5.

Ninguna es `autouse`. Los modulos que las necesitan las piden explicitamente con
`pytestmark = pytest.mark.usefixtures(...)`, de modo que el alcance sea el mismo
que tenian cuando estaban duplicadas: activarlas para toda la suite cambiaria el
comportamiento de las pruebas que hoy no las usan.
"""

import asyncio
import re

import httpx
import pytest

from app.adapters import tnlcm
from app.core.config import settings
from app.services import background, state

# Niveles de la piramide. Cada uno es un subdirectorio de `tests/` y da nombre a
# un marcador, de modo que `pytest -m unit` sea el ciclo corto de desarrollo.
LEVELS = frozenset({"unit", "integration", "adapters", "api", "contract", "system", "golden"})


def pytest_collection_modifyitems(config, items):
    """Marca cada prueba con el nivel del directorio donde vive.

    El directorio es la unica fuente de verdad: mover un fichero de `unit/` a
    `integration/` cambia su marcador sin tocar el fichero, y es imposible que
    una prueba quede sin clasificar o mal etiquetada.
    """
    for item in items:
        level = item.path.parent.name
        if level in LEVELS:
            item.add_marker(getattr(pytest.mark, level))


@pytest.fixture
def isolate_artifacts_dir(tmp_path):
    """Redirige `settings.artifacts_dir` a un temporal y lo restaura al salir."""
    previous = settings.artifacts_dir
    settings.artifacts_dir = str(tmp_path)
    yield tmp_path
    settings.artifacts_dir = previous


@pytest.fixture
def isolate_orchestrator_state(monkeypatch):
    """Aisla el estado global del orquestador y neutraliza las tasks de fondo.

    Devuelve la lista de nombres de las tasks que se habrian lanzado, para poder
    comprobar que arranca la fase correcta sin llegar a ejecutarla.
    """
    monkeypatch.setattr(state, "executions", {})
    monkeypatch.setattr(state, "save_to_disk", lambda: None)

    spawned: list[str] = []

    def _fake_spawn(coro, *, name: str):
        coro.close()
        spawned.append(name)
        return None

    monkeypatch.setattr(background, "spawn_background_task", _fake_spawn)
    return spawned


@pytest.fixture
def tnlcm_token(monkeypatch) -> str:
    """Deja un token de TNLCM cargado en memoria durante la prueba.

    El adaptador guarda el token en globales de modulo (deuda conocida: §9 Fase 2
    del roadmap), asi que hay que ponerlo y restaurarlo explicitamente.
    """
    token = "test-token"
    monkeypatch.setattr(tnlcm, "_tnlcm_access_token", token)
    monkeypatch.setattr(tnlcm, "_tnlcm_refresh_token", None)
    return token


class FakeHttp:
    """Servidor HTTP simulado: declara respuestas y registra lo que se le pidio.

    Sustituye el **transporte** de httpx, no el cliente. La diferencia importa:
    con un cliente falso hay que reimplementar `post`, `get`, `__aenter__`... y
    las pruebas quedan atadas a la interfaz de httpx. Con `MockTransport` corre
    httpx de verdad —construccion de la URL, cabeceras, `raise_for_status`,
    codificacion multipart— y lo unico simulado es la red. Lo que se afirma pasa
    a ser el contrato de cable con TNLCM/ELCM, que no cambia al mover un modulo.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._rules: list[tuple[str | None, int, dict]] = []
        self._queue: list[tuple[int, dict]] = []
        self._failure: BaseException | None = None

    @staticmethod
    def _body(json=None, text=None, content=None) -> dict:
        if json is not None:
            return {"json": json}
        if text is not None:
            return {"text": text}
        if content is not None:
            return {"content": content}
        return {}

    def respond(
        self,
        status_code: int = 200,
        *,
        json=None,
        text=None,
        content=None,
        when=None,
        method=None,
    ):
        """Declara una respuesta.

        `when` la limita a las URL que contengan esa subcadena y `method` al verbo
        indicado. Las reglas se evaluan en orden de declaracion.
        """
        self._rules.append((method, when, status_code, self._body(json, text, content)))
        return self

    def respond_once(self, status_code: int = 200, *, json=None, text=None, content=None):
        """Respuesta de un solo uso; util para probar reintentos."""
        self._queue.append((status_code, self._body(json, text, content)))
        return self

    def fail_with(self, error: BaseException):
        """Hace que el transporte reviente en vez de responder.

        Es la unica forma de ejercitar los fallos de transporte -conexion
        rechazada, timeout de conexion-, que httpx levanta como excepcion y
        nunca entrega como respuesta.
        """
        self._failure = error
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._failure is not None:
            raise self._failure
        if self._queue:
            status_code, body = self._queue.pop(0)
            return httpx.Response(status_code, request=request, **body)
        for method, when, status_code, body in self._rules:
            if method is not None and request.method != method.upper():
                continue
            if when is None or when in str(request.url):
                return httpx.Response(status_code, request=request, **body)
        raise AssertionError(f"sin respuesta declarada para {request.method} {request.url}")

    # --- lo que se afirma en las pruebas ---

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def paths_for(self, method: str) -> list[str]:
        """Rutas pedidas con ese verbo, en orden."""
        return [r.url.path for r in self.requests if r.method == method.upper()]

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "no se realizo ninguna peticion"
        return self.requests[-1]

    def multipart(self, request: httpx.Request | None = None) -> dict[str, tuple[str | None, str]]:
        """Descompone un cuerpo multipart en `{campo: (filename, valor)}`.

        Se lee del cuerpo REAL que viaja por el cable, no de los argumentos que
        se pasaron a httpx: es exactamente lo que recibe ELCM.
        """
        request = request or self.last
        boundary = request.headers.get("content-type", "").split("boundary=")[-1]
        fields: dict[str, tuple[str | None, str]] = {}
        for chunk in request.content.decode("utf-8", "replace").split(f"--{boundary}"):
            if "Content-Disposition" not in chunk:
                continue
            head, _, body = chunk.partition("\r\n\r\n")
            name = re.search(r'name="([^"]*)"', head)
            filename = re.search(r'filename="([^"]*)"', head)
            if name:
                fields[name.group(1)] = (
                    filename.group(1) if filename else None,
                    body.rstrip("\r\n-"),
                )
        return fields


@pytest.fixture
def sleepless(monkeypatch) -> list[float]:
    """Anula las esperas reales y devuelve los segundos que se pidieron dormir.

    Permite afirmar sobre la politica temporal (ventanas de registro, backoff)
    sin que la suite pague el tiempo.
    """
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return slept


@pytest.fixture
def fake_http(monkeypatch) -> FakeHttp:
    """Enchufa un `httpx.MockTransport` a todo cliente httpx que se cree."""
    server = FakeHttp()
    transport = httpx.MockTransport(server.handle)
    real_async, real_sync = httpx.AsyncClient, httpx.Client

    # `**kwargs` a proposito: si produccion anade un argumento al constructor
    # (headers, follow_redirects...), esto sigue funcionando en vez de romper.
    def async_client(**kwargs):
        kwargs.pop("transport", None)
        return real_async(transport=transport, **kwargs)

    def sync_client(**kwargs):
        kwargs.pop("transport", None)
        return real_sync(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(httpx, "Client", sync_client)
    return server
