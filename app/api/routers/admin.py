"""Recarga en caliente de la configuracion mutable."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import verify_api_key
from app.core import config as core_config
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["config"], dependencies=[Depends(verify_api_key)])


@router.post("/refresh")
async def post_refresh_config():
    """Recarga en caliente solo variables de configuracion mutables.

    Nota: este endpoint se renombró desde /login a /refresh.
    """
    try:
        result = core_config.reload_mutable_settings()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    logging.getLogger().setLevel(settings.log_level)
    logger.info("Configuracion recargada. Campos actualizados: %s", result["updated_fields"])
    return {
        "status": "reloaded",
        "updated_fields": result["updated_fields"],
        "non_reloadable_fields": result["non_reloadable_fields"],
    }
