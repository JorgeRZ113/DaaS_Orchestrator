from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr


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



class InfrastructureConfig(BaseModel):
    name: str = Field(..., description="Nombre del escenario o TN")
    descriptor_path: Optional[str] = Field(
        default=None,
        description="Ruta o referencia al descriptor que consumira TNLCM",
    )
    component: Optional[Dict[str, Dict[str, Any]]] = Field(
        default=None,
        description="Componentes y sus valores (ej: component.base para plantilla base)",
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Parametros extra para el despliegue",
    )

    def tnlcm_template_ref(self) -> str | None:
        """Devuelve la referencia seleccionada para el template TNLCM."""
        for key in ("template_tnlcm", "template", "descriptor", "descriptor_path"):
            value = self.parameters.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if self.descriptor_path:
            return self.descriptor_path.strip()
        return None

    def tnlcm_data_values(self) -> dict[str, Any]:
        """Devuelve el DataDescriptor bruto que se usará para poblar overlays.

        Busca en este orden:
        1. component.* (todas las subsecciones dict presentes)
        2. parameters.data_descriptor
        3. parameters.values
        4. parameters.data
        5. parameters completo
        """
        if self.component and isinstance(self.component, dict):
            merged_components: dict[str, Any] = {}
            for component_values in self.component.values():
                if isinstance(component_values, dict):
                    for key, value in component_values.items():
                        merged_components[key] = value
            if merged_components:
                return merged_components

        # Luego buscar en parameters
        candidate: Any = None
        for key in ("data_descriptor", "values", "data"):
            value = self.parameters.get(key)
            if isinstance(value, dict):
                candidate = value
                break
        if candidate is None:
            candidate = self.parameters

        if not isinstance(candidate, dict):
            return {}

        reserved = {
            "template_tnlcm",
            "template",
            "descriptor",
            "descriptor_path",
            "data_descriptor",
            "values",
            "data",
        }
        return {key: value for key, value in candidate.items() if key not in reserved}


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
    auto_start_elcm: bool = Field(
        default=True, description="Si True, inicia automáticamente ELCM al completar TNLCM"
    )

    def tnlcm_template_ref(self) -> str | None:
        return self.infrastructure.tnlcm_template_ref()

    def tnlcm_data_values(self) -> dict[str, Any]:
        return self.infrastructure.tnlcm_data_values()



class ExecutionRecord(BaseModel):
    execution_id: str
    status: ExecutionState
    message: str = ""
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


class ExecutionResponse(BaseModel):
    execution_id: str
    status: ExecutionState
    message: str = ""
