from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ExecutionState(str, Enum):
    pending = "PENDING"
    validating = "VALIDATING"
    deploying = "DEPLOYING"
    running_experiment = "RUNNING_EXPERIMENT"
    collecting = "COLLECTING"
    completed = "COMPLETED"
    failed = "FAILED"
    cancelled = "CANCELLED"


class RegisterRequest(BaseModel):
    email: str
    username: str
    password: str
    org: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    access_token: str


class TrialNetworkCreateRequest(BaseModel):
    descriptor: Dict[str, Any] = Field(
        ..., description="Descriptor de TNLCM para crear la Trial Network"
    )
    library_reference_type: str = Field(
        ..., description="Tipo de referencia en la libreria de TNLCM"
    )
    library_reference_value: str = Field(
        ..., description="Valor de referencia en la libreria de TNLCM"
    )
    tn_id: Optional[str] = Field(default=None, description="Identificador opcional de la TN")


class TNIdRequest(BaseModel):
    tn_id: str


class ActivateRequest(TNIdRequest):
    jenkins_deploy_pipeline: Optional[str] = Field(
        default=None,
        description="Pipeline opcional para activar/desplegar componentes",
    )


class ReportRequest(TNIdRequest):
    pass


class DestroyRequest(TNIdRequest):
    pass


class PurgeRequest(TNIdRequest):
    pass


class InfrastructureConfig(BaseModel):
    name: str = Field(..., description="Nombre del escenario o TN")
    descriptor_path: Optional[str] = Field(
        default=None,
        description="Ruta o referencia al descriptor que consumira TNLCM",
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Parametros extra para el despliegue",
    )


class ExperimentConfig(BaseModel):
    name: str = Field(
        ..., description="Nombre local del experimento para asociarlo luego con experiment_id"
    )
    testcase_paths: List[str] = Field(
        default_factory=list,
        description="Lista de rutas/referencias de TestCases para ejecutar en orden",
    )
    ues_paths: List[str] = Field(
        default_factory=list,
        description="Lista de rutas/referencias de UEs a usar en el experimento",
    )



class RunExperimentRequest(BaseModel):
    descriptor: ExperimentConfig


class DatasetRequest(BaseModel):
    output: Literal["logs"] = Field(
        default="logs",
        description="Por ahora solo se permite la recoleccion de logs",
    )


class DatasetDescriptor(BaseModel):
    infrastructure: InfrastructureConfig
    experiment: ExperimentConfig
    dataset: DatasetRequest = Field(default_factory=DatasetRequest)


class TnInitInfo(BaseModel):
    opennebula_vnet_id: Optional[str] = None
    vxlan: Optional[str] = None


class BastionInfo(BaseModel):
    private_key: Optional[str] = None
    wireguard_client_config: Optional[str] = None


class InfluxInfo(BaseModel):
    ip: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None


class GrafanaInfo(BaseModel):
    ip: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None


class ElcmInfo(BaseModel):
    ip: Optional[str] = None
    backend_port: Optional[int] = None
    frontend_port: Optional[int] = None


class ComponentInfo(BaseModel):
    name: str
    ip: Optional[str] = None
    port: Optional[int] = None
    ports: Dict[str, int] = Field(default_factory=dict)
    username: Optional[str] = None
    password: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ReportResponse(BaseModel):
    tn_id: str
    raw: Optional[str] = None
    markdown: Optional[str] = None

    tn_init: Optional[TnInitInfo] = None
    bastion: Optional[BastionInfo] = None
    influx: Optional[InfluxInfo] = None
    grafana: Optional[GrafanaInfo] = None
    elcm: Optional[ElcmInfo] = None

    components: Dict[str, ComponentInfo] = Field(default_factory=dict)


class ExecutionRecord(BaseModel):
    execution_id: str
    status: ExecutionState
    message: str = ""
    tn_id: Optional[str] = None
    experiment_id: Optional[str] = None
    experiment_ids: List[str] = Field(default_factory=list)
    elcm_execution_id: Optional[str] = None  # ID de la ejecución en ELCM
    artifacts: List[str] = Field(default_factory=list)
    error: Optional[str] = None


class ExecutionResponse(BaseModel):
    execution_id: str
    status: ExecutionState
    message: str = ""
