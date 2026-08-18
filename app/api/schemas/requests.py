"""Cuerpos de peticion de la API del orquestador."""

from pydantic import BaseModel, Field

from app.domain.descriptor import DatasetRequest, ExperimentConfig


class ElcmExperimentRequest(BaseModel):
    experiment: ExperimentConfig
    dataset: DatasetRequest = Field(
        default_factory=DatasetRequest,
        description=(
            "Formato(s) de entrega del dataset para ESTE experimento. Cada "
            "llamada a /elcm puede pedir una salida distinta; por defecto 'logs'."
        ),
    )
