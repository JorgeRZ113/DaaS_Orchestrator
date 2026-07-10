"""Utilities for downstream retries, backoff and latency measurement.

The project already uses service-specific retry logic in TNLCM/ELCM adapters.
This module provides a reusable async helper so the retry policy can be
centralized when the codebase is ready to adopt it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    """Configuration for async retries."""

    max_attempts: int = 2
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    jitter: bool = True
    retry_exceptions: tuple[type[BaseException], ...] = (asyncio.TimeoutError,)


@dataclass(frozen=True)
class LatencySample:
    """Simple latency sample for downstream calls."""

    operation: str
    elapsed_seconds: float


async def with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    operation_name: str = "downstream call",
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> T:
    """Execute an async callable with exponential backoff and optional jitter.

    The helper retries only the exception types configured in ``policy``.
    It is intentionally generic so the caller can decide which HTTP statuses
    or business errors should be retried.
    """

    retry_policy = policy or RetryPolicy()
    if retry_policy.max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if retry_policy.base_delay_seconds < 0:
        raise ValueError("base_delay_seconds must be >= 0")
    if retry_policy.max_delay_seconds < retry_policy.base_delay_seconds:
        raise ValueError("max_delay_seconds must be >= base_delay_seconds")

    last_exc: BaseException | None = None

    for attempt in range(1, retry_policy.max_attempts + 1):
        try:
            return await operation()
        except retry_policy.retry_exceptions as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt >= retry_policy.max_attempts:
                break

            delay = min(
                retry_policy.max_delay_seconds,
                retry_policy.base_delay_seconds * (2 ** (attempt - 1)),
            )
            if retry_policy.jitter and delay > 0:
                delay = random.uniform(0, delay)

            logger.warning(
                "%s failed on attempt %s/%s; retrying in %.3fs",
                operation_name,
                attempt,
                retry_policy.max_attempts,
                delay,
            )
            await sleep(delay)

    assert last_exc is not None
    raise last_exc


async def timed(
    operation: Callable[[], Awaitable[T]], operation_name: str
) -> tuple[T, LatencySample]:
    """Run an async callable and return both its result and elapsed time."""

    start = time.perf_counter()
    result = await operation()
    elapsed = time.perf_counter() - start
    sample = LatencySample(operation=operation_name, elapsed_seconds=elapsed)
    logger.info("%s completed in %.3fs", operation_name, elapsed)
    return result, sample
