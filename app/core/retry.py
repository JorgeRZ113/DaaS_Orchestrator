"""Mecanismo unico de reintentos del proyecto.

Todo el "como se reintenta" (bucle, calculo del retardo, decision de si un error
merece otro intento) vive aqui. Los adaptadores solo eligen una politica del
catalogo del final del modulo y, si lo necesitan, pasan dos ganchos:

  * ``veto``: corta el reintento mirando algo que la politica no puede saber
    (p. ej. el cuerpo de la respuesta). Se pasa en la llamada y no en la
    politica a proposito: el veto de TNLCM necesita ``_is_no_such_file_error``,
    que vive en ``app/tnlcm.py``, y ese modulo ya importa este -> referenciarlo
    desde el catalogo crearia un import circular. Manteniendolo fuera, el
    catalogo son datos puros (numeros y codigos de estado).
  * ``on_retry``: se invoca antes de cada espera. Es lo que permite emitir
    telemetria por intento sin que este modulo (transversal) importe
    ``app.observability.telemetry``.

Regla que ningun llamante debe tener que comprobar: ``run`` **nunca envuelve la
excepcion**. Si se agotan los intentos, o si el error no es reintentable, se
relanza el original con su traza intacta. Hay codigo que depende de ello (el
fallback al endpoint legacy de TNLCM captura ``httpx.HTTPStatusError`` para
mirar su codigo 404/405).

Uso:
    from app.core import retry

    async def _call():
        response = await client.put(url)
        response.raise_for_status()
        return response

    await retry.TNLCM_ACTIVATE.run(_call, veto=_veto, on_retry=_on_retry)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, TypeVar

import httpx

T = TypeVar("T")
logger = logging.getLogger(__name__)

BackoffMode = Literal["linear", "exponential"]


@dataclass(frozen=True)
class Attempt:
    """Contexto del intento que acaba de fallar, entregado al gancho `on_retry`."""

    number: int
    """Numero del intento fallido, empezando en 1."""

    max_attempts: int

    delay_seconds: float
    """Lo que se va a esperar antes del siguiente intento."""

    error: BaseException


Veto = Callable[[BaseException], bool]
OnRetry = Callable[[Attempt], None]


class RetryPolicy:
    """Politica de reintentos: cuantos intentos, cuanto se espera y que se reintenta.

    Una instancia es inmutable en la practica y se comparte entre llamadas: no
    guarda estado del intento en curso, todo vive en la pila de `run`.
    """

    def __init__(
        self,
        name: str,
        *,
        max_attempts: int,
        base_delay_seconds: float,
        mode: BackoffMode = "linear",
        increment_seconds: float = 0.0,
        max_delay_seconds: float = 60.0,
        retry_on: tuple[type[BaseException], ...] = (),
        retry_statuses: frozenset[int] = frozenset(),
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"{name}: max_attempts must be >= 1")
        if base_delay_seconds < 0:
            raise ValueError(f"{name}: base_delay_seconds must be >= 0")
        if increment_seconds < 0:
            raise ValueError(f"{name}: increment_seconds must be >= 0")
        if max_delay_seconds < base_delay_seconds:
            raise ValueError(f"{name}: max_delay_seconds must be >= base_delay_seconds")
        if not all(isinstance(item, type) and issubclass(item, BaseException) for item in retry_on):
            raise TypeError(f"{name}: retry_on must be a tuple of exception types")

        self.name = name
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.mode: BackoffMode = mode
        self.increment_seconds = increment_seconds
        self.max_delay_seconds = max_delay_seconds
        self.retry_on = retry_on
        self.retry_statuses = retry_statuses

    def __repr__(self) -> str:
        return (
            f"RetryPolicy({self.name!r}, max_attempts={self.max_attempts}, "
            f"mode={self.mode!r}, base_delay_seconds={self.base_delay_seconds})"
        )

    def delay_for(self, attempt_number: int) -> float:
        """Segundos a esperar tras fallar el intento `attempt_number` (1-based).

        `linear` -> base + (n-1) * incremento (retardos deterministas y previsibles).
        `exponential` -> base * 2^(n-1). Ambos topados por `max_delay_seconds`.
        """
        if attempt_number < 1:
            raise ValueError("attempt_number must be >= 1")

        if self.mode == "linear":
            delay = self.base_delay_seconds + ((attempt_number - 1) * self.increment_seconds)
        else:
            delay = self.base_delay_seconds * (2 ** (attempt_number - 1))

        return min(delay, self.max_delay_seconds)

    def is_retryable(self, error: BaseException, veto: Veto | None = None) -> bool:
        """Decide si `error` merece otro intento.

        Tres filtros en cadena: el tipo debe estar declarado en `retry_on`, el
        `veto` del llamante no debe cortarlo y, si es un error de estado HTTP,
        el codigo debe estar en `retry_statuses`.
        """
        if not isinstance(error, self.retry_on):
            return False
        if veto is not None and veto(error):
            return False
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code in self.retry_statuses
        return True

    async def run(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        veto: Veto | None = None,
        on_retry: OnRetry | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ) -> T:
        """Ejecuta `operation` reintentando segun esta politica.

        Relanza SIEMPRE la excepcion original, sin envolverla, tanto si se agotan
        los intentos como si el error no era reintentable.

        `sleep` es inyectable para testear sin esperas reales. Cuando no se pasa
        se resuelve `asyncio.sleep` en cada llamada, y no como valor por defecto
        del parametro: un default se enlazaria al importar el modulo y dejaria
        fuera de juego a los tests que parchean `asyncio.sleep`.
        """
        sleep_fn = sleep if sleep is not None else asyncio.sleep
        for attempt_number in range(1, self.max_attempts + 1):
            try:
                return await operation()
            except self.retry_on as error:
                exhausted = attempt_number >= self.max_attempts
                if exhausted or not self.is_retryable(error, veto):
                    raise

                delay_seconds = self.delay_for(attempt_number)
                attempt = Attempt(
                    number=attempt_number,
                    max_attempts=self.max_attempts,
                    delay_seconds=delay_seconds,
                    error=error,
                )
                if on_retry is not None:
                    on_retry(attempt)
                else:
                    logger.warning(
                        "%s failed on attempt %s/%s; retrying in %ss",
                        self.name,
                        attempt_number,
                        self.max_attempts,
                        delay_seconds,
                    )
                await sleep_fn(delay_seconds)

        # Inalcanzable: el ultimo intento del bucle siempre retorna o relanza.
        raise RuntimeError(f"{self.name}: retry loop ended without result")


# ============================================================================
# Catalogo de politicas
# ----------------------------------------------------------------------------
# Todos los reintentos del proyecto se declaran aqui. Anadir uno nuevo es anadir
# una constante, no escribir otro bucle.
# ============================================================================

# TimeoutException es subclase de TransportError; se listan las dos porque los
# llamantes distinguen ambos casos en su telemetria.
HTTP_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPStatusError,
    httpx.TimeoutException,
    httpx.TransportError,
)

HTTP_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})


TNLCM_ACTIVATE = RetryPolicy(
    "tnlcm.activate",
    max_attempts=3,
    base_delay_seconds=1.0,
    mode="linear",
    increment_seconds=1.0,
    retry_on=HTTP_TRANSIENT_ERRORS,
    retry_statuses=HTTP_RETRYABLE_STATUSES,
)
"""Activacion de una TN: retardos deterministas de 1 s y 2 s."""


ELCM_RUN = RetryPolicy(
    "elcm.run",
    max_attempts=3,
    base_delay_seconds=2.0,
    mode="exponential",
    max_delay_seconds=30.0,
    retry_on=HTTP_TRANSIENT_ERRORS,
    retry_statuses=HTTP_RETRYABLE_STATUSES,
)
"""Lanzamiento de un experimento en ELCM: 2 s y 4 s."""
