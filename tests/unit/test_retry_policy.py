"""El mecanismo unico de reintentos: retardos, criterio y bucle.

`RetryPolicy` concentra el "como se reintenta" de todo el proyecto, asi que
merece pruebas propias en vez de comprobarse de rebote a traves de TNLCM o ELCM.

La garantia que mas depende de estas pruebas es que `run` **nunca envuelve la
excepcion original**: hay codigo que captura `httpx.HTTPStatusError` para mirar
su codigo y caer al endpoint legacy, y envolverla romperia ese camino en
silencio.
"""

import asyncio

import httpx
import pytest

from app.core.retry import ELCM_RUN, TNLCM_ACTIVATE, Attempt, RetryPolicy


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("PUT", "http://tnlcm.local/activate")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _policy(**overrides) -> RetryPolicy:
    defaults = dict(
        max_attempts=3,
        base_delay_seconds=1.0,
        increment_seconds=1.0,
        mode="linear",
        retry_on=(httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError),
        retry_statuses=frozenset({500, 503}),
    )
    return RetryPolicy("test", **{**defaults, **overrides})


async def _never_sleeps(_seconds) -> None:
    pass


# --- calculo del retardo ------------------------------------------------------


def test_linear_backoff_adds_a_fixed_increment():
    policy = _policy(base_delay_seconds=1.0, increment_seconds=1.0)

    assert [policy.delay_for(n) for n in (1, 2, 3)] == [1.0, 2.0, 3.0]


def test_exponential_backoff_doubles():
    policy = _policy(mode="exponential", base_delay_seconds=2.0)

    assert [policy.delay_for(n) for n in (1, 2, 3)] == [2.0, 4.0, 8.0]


def test_backoff_is_capped_by_max_delay():
    policy = _policy(mode="exponential", base_delay_seconds=2.0, max_delay_seconds=5.0)

    assert policy.delay_for(10) == 5.0


def test_delay_for_rejects_attempt_numbers_below_one():
    with pytest.raises(ValueError):
        _policy().delay_for(0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"base_delay_seconds": -1},
        {"increment_seconds": -1},
        {"max_delay_seconds": 0.1},  # menor que base_delay_seconds
    ],
)
def test_invalid_policies_fail_at_construction(kwargs):
    """Una politica mal configurada tiene que romper al declararla, no al usarla."""
    with pytest.raises(ValueError):
        _policy(**kwargs)


def test_retry_on_must_contain_exception_types():
    with pytest.raises(TypeError):
        _policy(retry_on=("no soy una excepcion",))


# --- criterio de reintento ----------------------------------------------------


def test_only_declared_status_codes_are_retryable():
    policy = _policy()

    assert policy.is_retryable(_http_error(503)) is True
    assert policy.is_retryable(_http_error(404)) is False


def test_timeouts_and_transport_errors_are_retryable_regardless_of_status():
    policy = _policy()

    assert policy.is_retryable(httpx.ReadTimeout("t")) is True
    assert policy.is_retryable(httpx.ConnectError("c")) is True


def test_undeclared_exception_types_are_never_retryable():
    assert _policy().is_retryable(ValueError("x")) is False


def test_veto_wins_over_a_retryable_status():
    """El veto existe para lo que la politica no puede saber: el cuerpo."""
    policy = _policy()

    assert policy.is_retryable(_http_error(503), veto=lambda _: True) is False


# --- bucle --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_the_result_of_the_first_successful_attempt():
    calls = []

    async def operation():
        calls.append(1)
        if len(calls) < 2:
            raise _http_error(503)
        return "ok"

    assert await _policy().run(operation, sleep=_never_sleeps) == "ok"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_exhausting_the_attempts_reraises_the_original_exception():
    calls = []

    async def operation():
        calls.append(1)
        raise _http_error(503)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await _policy().run(operation, sleep=_never_sleeps)

    # Ni envuelta ni sustituida: el llamante necesita el codigo de estado.
    assert exc_info.value.response.status_code == 503
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_a_non_retryable_error_fails_on_the_first_attempt():
    calls = []

    async def operation():
        calls.append(1)
        raise _http_error(404)

    with pytest.raises(httpx.HTTPStatusError):
        await _policy().run(operation, sleep=_never_sleeps)

    assert len(calls) == 1  # no se malgasta un segundo intento


@pytest.mark.asyncio
async def test_on_retry_receives_the_context_of_each_failed_attempt():
    seen: list[Attempt] = []

    async def operation():
        raise _http_error(503)

    with pytest.raises(httpx.HTTPStatusError):
        await _policy().run(operation, on_retry=seen.append, sleep=_never_sleeps)

    assert [a.number for a in seen] == [1, 2]  # el ultimo fallo ya no reintenta
    assert [a.delay_seconds for a in seen] == [1.0, 2.0]
    assert all(a.max_attempts == 3 for a in seen)
    assert all(isinstance(a.error, httpx.HTTPStatusError) for a in seen)


@pytest.mark.asyncio
async def test_waits_the_declared_delay_between_attempts():
    slept: list[float] = []

    async def operation():
        raise _http_error(503)

    async def record(seconds):
        slept.append(seconds)

    with pytest.raises(httpx.HTTPStatusError):
        await _policy().run(operation, sleep=record)

    assert slept == [1.0, 2.0]


@pytest.mark.asyncio
async def test_cancellation_is_never_swallowed_nor_retried():
    """`CancelledError` no hereda de Exception: apagar el servicio debe cortar ya."""
    calls = []

    async def operation():
        calls.append(1)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _policy().run(operation, sleep=_never_sleeps)

    assert len(calls) == 1


# --- casos que anadieron las pruebas de mutacion ------------------------------
#
# Las siguientes existen porque `cosmic-ray` demostro que sin ellas la suite no
# distinguia mutaciones reales del codigo, pese al 97 % de cobertura de lineas.


def test_linear_increment_is_multiplied_not_divided():
    """Con incremento 1, `*`, `/` y `//` dan lo mismo: hay que usar otro valor.

    La prueba original usaba incremento 1.0 y por eso no distinguia la
    multiplicacion de la division: 1*1 == 1/1 == 1//1.
    """
    policy = _policy(base_delay_seconds=1.0, increment_seconds=3.0)

    assert [policy.delay_for(n) for n in (1, 2, 3)] == [1.0, 4.0, 7.0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 1},  # un solo intento es valido
        {"base_delay_seconds": 0.0},  # reintentar sin esperar es valido
        {"max_delay_seconds": 1.0, "base_delay_seconds": 1.0},  # tope == base
    ],
)
def test_boundary_values_are_accepted(kwargs):
    """Fija el limite exacto: sin esto, `< 1` y `<= 1` son indistinguibles."""
    assert _policy(**kwargs) is not None


def test_attempt_context_is_immutable():
    """`Attempt` viaja a un hook ajeno: no debe poder alterarlo."""
    attempt = Attempt(number=1, max_attempts=3, delay_seconds=1.0, error=ValueError())

    with pytest.raises(Exception):
        attempt.number = 99


def test_catalog_policies_keep_their_declared_configuration():
    """El catalogo es configuracion de produccion: sus valores son el contrato.

    Sin esto, cambiar un codigo de estado o un numero de intentos no rompia nada
    en la suite.
    """
    assert TNLCM_ACTIVATE.max_attempts == 3
    assert TNLCM_ACTIVATE.mode == "linear"
    assert [TNLCM_ACTIVATE.delay_for(n) for n in (1, 2)] == [1.0, 2.0]
    assert TNLCM_ACTIVATE.retry_statuses == frozenset({500, 502, 503, 504})

    assert ELCM_RUN.max_attempts == 3
    assert ELCM_RUN.mode == "exponential"
    assert [ELCM_RUN.delay_for(n) for n in (1, 2)] == [2.0, 4.0]
    assert ELCM_RUN.max_delay_seconds == 30.0
    assert ELCM_RUN.retry_statuses == frozenset({500, 502, 503, 504})


def test_default_policy_values():
    """Los defaults del constructor tambien son contrato."""
    policy = RetryPolicy("defaults", max_attempts=2, base_delay_seconds=1.0)

    assert policy.mode == "linear"
    assert policy.increment_seconds == 0.0  # sin incremento, retardo constante
    assert policy.max_delay_seconds == 60.0
    assert [policy.delay_for(n) for n in (1, 2, 3)] == [1.0, 1.0, 1.0]


def test_retry_on_rejects_a_non_exception_even_if_the_first_check_passes():
    """`and` y `or` en la validacion de tipos no son intercambiables."""
    with pytest.raises(TypeError):
        _policy(retry_on=(int,))  # es un tipo, pero no una excepcion
