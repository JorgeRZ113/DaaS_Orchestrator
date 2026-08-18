"""Lanzamiento supervisado de tareas en segundo plano.

Regla §8.2: prohibido `asyncio.create_task` sin retener la referencia. Sin ella
el recolector de basura puede llevarse la task a mitad de ejecucion y su
excepcion no la ve nadie.
"""

import asyncio
import logging
from typing import Coroutine

logger = logging.getLogger(__name__)

# Retiene las referencias vivas (§8.2).
_background_tasks: set[asyncio.Task] = set()


def spawn_background_task(coro: Coroutine, *, name: str) -> asyncio.Task:
    """Lanza una task supervisada: retiene la referencia y loguea excepciones."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _on_done(finished: asyncio.Task) -> None:
        _background_tasks.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            logger.error(
                "Background task %s failed: %s",
                name,
                finished.exception(),
                exc_info=finished.exception(),
            )

    task.add_done_callback(_on_done)
    return task
