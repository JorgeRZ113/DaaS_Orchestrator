"""Contratos de datos del adaptador TNLCM.

Describen lo que viaja HACIA y DESDE TNLCM, no la API que expone este servicio;
por eso viven en `adapters` y no en `domain`.

Los payloads salientes (`RegisterRequest`, `ActivateRequest`) se serializan
siempre con `model_dump(exclude_none=True)`: TNLCM espera que las claves
opcionales se omitan, no que lleguen a null.
"""

import logging
from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


class RegisterRequest(BaseModel):
    """Body de `POST /api/v1/user/register` de TNLCM (saliente)."""

    username: str
    password: str
    email: Optional[str] = None
    org: Optional[str] = None


class TokenPair(BaseModel):
    """Tokens devueltos por `POST /api/v1/user/login` de TNLCM (entrante)."""

    # `populate_by_name` hace falta porque `access_token` lleva alias de
    # validacion: sin el, construir el modelo por nombre de campo fallaria.
    model_config = ConfigDict(populate_by_name=True)

    access_token: str = Field(validation_alias=AliasChoices("access_token", "token"))
    refresh_token: Optional[str] = None

    @classmethod
    def from_login_response(cls, data: dict[str, Any]) -> "TokenPair":
        """Normaliza las formas en que TNLCM devuelve los tokens.

        Segun el despliegue, los tokens llegan en la raiz o anidados bajo `data`,
        y el de acceso puede llamarse `access_token` o `token`.

        El cuerpo de la respuesta NO viaja en el error: contiene los tokens y
        acabaria en `executions.json` y en los logs a traves de `str(exc)`
        (regla de no persistir secretos). Solo se deja en DEBUG.
        """
        nested = data.get("data") or {}
        payload = {**nested, **data} if isinstance(nested, dict) else data
        try:
            tokens = cls.model_validate(payload)
        except ValidationError as exc:
            logger.debug("Unexpected TNLCM login response shape", exc_info=True)
            raise ValueError("TNLCM login did not return a usable access_token") from exc

        access_token = tokens.access_token.strip()
        if not access_token:
            raise ValueError("TNLCM returned an empty access_token")

        refresh_token = tokens.refresh_token.strip() if tokens.refresh_token else None
        return cls(access_token=access_token, refresh_token=refresh_token or None)


class TNIdRequest(BaseModel):
    """Identificador de TN que TNLCM espera en el body de sus endpoints legacy."""

    tn_id: str


class ActivateRequest(TNIdRequest):
    """Body de activacion de una TN (saliente).

    `jenkins_deploy_pipeline` es la evidencia de que TNLCM delega el despliegue
    real en Jenkins; su valor sale de `infrastructure.parameters`.
    """

    jenkins_deploy_pipeline: Optional[str] = Field(
        default=None,
        description="Pipeline opcional para activar/desplegar componentes",
    )
