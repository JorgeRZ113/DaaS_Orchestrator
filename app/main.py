"""Raiz de composicion: monta la aplicacion FastAPI y arranca el servidor.

Aqui no vive logica de negocio ni ningun endpoint. Lo unico que hace este modulo
es decidir QUE routers componen la API y en que orden, de modo que la forma del
servicio se lea de un vistazo.

`app` se expone a nivel de modulo porque es lo que consumen uvicorn
(`app.main:app`) y los tests (`TestClient(app)`).
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.routers import admin, auth, executions, experiments, health
from app.core.config import settings

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"DaaS Orchestrator arrancando en modo {settings.app_env}")
    yield
    logger.info("DaaS Orchestrator apagandose")


def create_app() -> FastAPI:
    """Construye la aplicacion registrando routers y manejadores de error."""
    application = FastAPI(
        title="DaaS Orchestrator",
        description="Capa de automatizacion para generacion de datasets en redes 5G/6G",
        version="0.1.0",
        lifespan=lifespan,
    )

    for router in (
        health.router,
        auth.router,
        admin.router,
        executions.router,
        experiments.router,
    ):
        application.include_router(router)

    register_error_handlers(application)
    return application


app = create_app()


def main() -> None:
    """Punto de entrada de consola (`daas-orchestrator`) para arrancar Uvicorn."""
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_env == "dev",
    )
