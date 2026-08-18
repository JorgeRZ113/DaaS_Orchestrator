"""Traduccion de errores no controlados a respuesta HTTP."""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(f"Error no controlado en {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(Exception, generic_exception_handler)
