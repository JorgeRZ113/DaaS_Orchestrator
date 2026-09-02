"""Estado vivo de una ejecucion: el registro que se persiste y sus experimentos.

`ExecutionRecord` es el agregado central del dominio y lo que se serializa a
`executions.json`; sus `PrivateAttr` (timer y eventos de fase) son estado del
proceso y quedan fuera de esa serializacion.
"""

import asyncio
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr

from app.domain.enums import ExecutionState


class ExperimentRun(BaseModel):
    """Registro de un experimento individual ejecutado sobre la TN."""

    name: str
    elcm_execution_id: Optional[str] = None
    status: Literal["RUNNING", "FINISHED", "FAILED"] = "RUNNING"
    # Formatos de entrega solicitados para este experimento concreto (dataset.output
    # del body de /elcm). Se recolectan en artifacts/<id>/result/<experimento>/.
    dataset_output: List[str] = Field(default_factory=lambda: ["logs"])
    # Variables globales del bloque dataset usadas en este experimento.
    dataset_variables: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class ExecutionRecord(BaseModel):
    execution_id: str
    status: ExecutionState
    message: str = ""
    ephemeral_tn: bool = False
    # Formatos de entrega solicitados (dataset.output), fijados al crear la
    # ejecución. La fase ELCM los consulta para saber qué recolectar/inyectar.
    dataset_output: List[str] = Field(default_factory=lambda: ["logs"])
    # Variables globales del bloque dataset fijadas al crear la ejecución. Se
    # reutilizan cuando POST /executions/{id}/elcm no trae bloque `dataset`.
    dataset_variables: Dict[str, Any] = Field(default_factory=dict)
    experiments: List[ExperimentRun] = Field(default_factory=list)
    tn_id: Optional[str] = None
    vpn_interface: Optional[str] = None
    vpn_conf_path: Optional[str] = None
    vpn_status: Optional[str] = None
    vpn_error: Optional[str] = None
    experiment_id: Optional[str] = None
    experiment_ids: List[str] = Field(default_factory=list)
    elcm_execution_id: Optional[str] = None  # ID de la ejecución en ELCM
    elcm_base_url: Optional[str] = None
    artifacts: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    _execution_timer: Any = PrivateAttr(default=None)

    # Señales de fase: la background task las activa al alcanzar un estado
    # terminal (haya ido bien o mal) y el endpoint bloqueante espera en ellas.
    # Son estado vivo del proceso, no se serializan a executions.json.
    _vpn_ready: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _experiment_finished: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _tn_purged: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
