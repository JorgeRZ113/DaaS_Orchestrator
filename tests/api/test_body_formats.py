"""Las tres vias de entrada del descriptor, a traves del endpoint real.

El anteproyecto compromete que el experimento se describa en un unico fichero
YAML. Aqui se comprueba que ese fichero entra tanto pegado en el cuerpo como
subido como adjunto, y que llega al orquestador exactamente igual que el JSON
equivalente.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.api.body_formats import MAX_BODY_BYTES
from app.core.config import settings
from app.domain.enums import ExecutionState
from app.domain.execution import ExecutionRecord
from app.main import app

client = TestClient(app)


def _headers(content_type: str | None = None) -> dict[str, str]:
    headers = {"x-api-key": settings.api_key}
    if content_type:
        headers["content-type"] = content_type
    return headers


DESCRIPTOR_PAYLOAD = {
    "infrastructure": {
        "name": "tn-formats",
        "component": {
            "base": {
                "influxdb_user": "admin",
                "influxdb_password": "adminadmin",
                "grafana_password": "adminadmin",
            }
        },
        "parameters": {
            "library_reference_type": "branch",
            "library_reference_value": "develop",
        },
    },
    "experiment": {"name": "exp-demo", "testcase_paths": ["TC_ping.yml"], "ues_paths": []},
    "dataset": {"output": ["logs"]},
    "auto_start_elcm": True,
}

DESCRIPTOR_YAML = """\
# Descriptor de la demo
infrastructure:
  name: tn-formats
  component:
    base:
      influxdb_user: admin
      influxdb_password: adminadmin
      grafana_password: adminadmin
  parameters:
    library_reference_type: branch
    library_reference_value: develop
experiment:
  name: exp-demo
  testcase_paths:
    - TC_ping.yml
  ues_paths: []
dataset:
  output: [logs]
auto_start_elcm: true
"""


@pytest.fixture
def captured(monkeypatch) -> dict:
    """Intercepta al orquestador y guarda el descriptor y el origen recibidos."""
    seen: dict = {}

    async def _capture(descriptor, source=None):
        seen["descriptor"] = descriptor
        seen["source"] = source
        return ExecutionRecord(
            execution_id=descriptor.infrastructure.name,
            status=ExecutionState.pending,
            message="accepted",
        )

    monkeypatch.setattr("app.services.orchestrator.create_tnlcm_execution", _capture)
    return seen


def _post_json() -> object:
    return client.post("/executions?wait=false", json=DESCRIPTOR_PAYLOAD, headers=_headers())


def _post_yaml_body(document: str = DESCRIPTOR_YAML) -> object:
    return client.post(
        "/executions?wait=false", content=document, headers=_headers("application/yaml")
    )


def _post_yaml_file(document: str = DESCRIPTOR_YAML) -> object:
    return client.post(
        "/executions?wait=false",
        files={"descriptor": ("descriptor.yaml", document.encode(), "application/yaml")},
        headers=_headers(),
    )


# --- las tres vias funcionan y son equivalentes ---


@pytest.mark.parametrize("post", [_post_json, _post_yaml_body, _post_yaml_file])
def test_every_input_format_is_accepted(post, captured) -> None:
    response = post()

    assert response.status_code == 202
    assert captured["descriptor"].infrastructure.name == "tn-formats"


def test_yaml_body_and_json_produce_the_same_descriptor(captured) -> None:
    _post_json()
    from_json = captured["descriptor"]

    _post_yaml_body()
    from_yaml = captured["descriptor"]

    assert from_json.model_dump() == from_yaml.model_dump()


def test_uploaded_file_and_json_produce_the_same_descriptor(captured) -> None:
    _post_json()
    from_json = captured["descriptor"]

    _post_yaml_file()
    from_file = captured["descriptor"]

    assert from_json.model_dump() == from_file.model_dump()


# --- el origen llega al orquestador para poder persistirlo ---


def test_json_source_is_reported_without_raw_text(captured) -> None:
    _post_json()

    assert captured["source"].format == "json"
    assert captured["source"].raw is None


@pytest.mark.parametrize("post", [_post_yaml_body, _post_yaml_file])
def test_yaml_source_carries_the_original_text(post, captured) -> None:
    post()

    assert captured["source"].format == "yaml"
    assert captured["source"].raw == DESCRIPTOR_YAML
    assert "# Descriptor de la demo" in captured["source"].raw


# --- errores propios del camino YAML ---


def test_malformed_yaml_returns_400_with_position(captured) -> None:
    response = _post_yaml_body("infrastructure:\n  name: tn\n   bad: indent\n")

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["line"] == 3
    assert "yaml_error" in detail


def test_empty_yaml_returns_400(captured) -> None:
    assert _post_yaml_body("").status_code == 400


def test_yaml_list_root_returns_400(captured) -> None:
    response = _post_yaml_body("- uno\n- dos\n")

    assert response.status_code == 400
    assert "mapping" in response.json()["detail"]["message"]


def test_multipart_without_descriptor_field_returns_400(captured) -> None:
    response = client.post(
        "/executions?wait=false",
        files={"otro_campo": ("descriptor.yaml", b"infrastructure:\n  name: x\n")},
        headers=_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"]["missing_field"] == "descriptor"


def test_oversized_body_returns_413(captured) -> None:
    padding = "x" * (MAX_BODY_BYTES + 1)
    response = _post_yaml_body(f"infrastructure:\n  name: tn\n  filler: {padding}\n")

    assert response.status_code == 413
    assert response.json()["detail"]["max_bytes"] == MAX_BODY_BYTES


def test_invalid_utf8_returns_400(captured) -> None:
    response = client.post(
        "/executions?wait=false", content=b"\xff\xfe not utf8", headers=_headers("application/yaml")
    )

    assert response.status_code == 400


# --- el contrato de validacion no cambia entre formatos ---


def test_schema_errors_are_identical_across_formats(captured) -> None:
    """Un descriptor incompleto debe fallar igual venga como venga."""
    broken = {"dataset": {"output": ["logs"]}}

    from_json = client.post("/executions?wait=false", json=broken, headers=_headers())
    from_yaml = _post_yaml_body("dataset:\n  output: [logs]\n")
    from_file = _post_yaml_file("dataset:\n  output: [logs]\n")

    assert from_json.status_code == 422
    assert from_yaml.status_code == 422
    assert from_file.status_code == 422
    assert from_json.json() == from_yaml.json() == from_file.json()


def test_business_validation_still_runs_on_yaml(captured) -> None:
    """Los 400 propios de la API (campos vacios) no se saltan por venir en YAML."""
    response = _post_yaml_body(DESCRIPTOR_YAML.replace("influxdb_user: admin", 'influxdb_user: ""'))

    assert response.status_code == 400
    assert "empty_fields" in response.json()["detail"]


# --- el contrato OpenAPI declara las tres vias ---


@pytest.mark.parametrize(
    "path,schema_ref",
    [
        ("/executions", "DatasetDescriptor"),
        ("/executions/{execution_id}/elcm", "ElcmExperimentRequest"),
    ],
)
def test_openapi_advertises_every_input_format(path: str, schema_ref: str) -> None:
    content = app.openapi()["paths"][path]["post"]["requestBody"]["content"]

    assert set(content) == {"application/json", "application/yaml", "multipart/form-data"}
    assert content["application/yaml"]["schema"] == {"$ref": f"#/components/schemas/{schema_ref}"}

    # El selector de fichero de Swagger depende de `format: binary`.
    descriptor_field = content["multipart/form-data"]["schema"]["properties"]["descriptor"]
    assert descriptor_field["type"] == "string"
    assert descriptor_field["format"] == "binary"


def test_openapi_keeps_the_model_reference() -> None:
    """Aceptar mas formatos no puede degradar el esquema que documenta el body."""
    spec = app.openapi()

    assert spec["paths"]["/executions"]["post"]["requestBody"]["content"]["application/json"] == {
        "schema": {"$ref": "#/components/schemas/DatasetDescriptor"}
    }
    assert "DatasetDescriptor" in spec["components"]["schemas"]


# --- /elcm admite lo mismo ---


ELCM_YAML = """\
experiment:
  name: exp-yaml
  testcase_paths:
    - TC_ping.yml
  ues_paths: []
dataset:
  output: [logs]
"""


@pytest.mark.parametrize("send_as_file", [False, True])
def test_elcm_endpoint_accepts_yaml(send_as_file: bool) -> None:
    """Llega a la logica de negocio: responde 404 por la ejecucion, no 422 por el formato."""
    if send_as_file:
        response = client.post(
            "/executions/no-existe/elcm?wait=false",
            files={"descriptor": ("experiment.yaml", ELCM_YAML.encode(), "application/yaml")},
            headers=_headers(),
        )
    else:
        response = client.post(
            "/executions/no-existe/elcm?wait=false",
            content=ELCM_YAML,
            headers=_headers("application/yaml"),
        )

    assert response.status_code == 404


def test_elcm_endpoint_still_accepts_json() -> None:
    response = client.post(
        "/executions/no-existe/elcm?wait=false",
        json=json.loads(
            json.dumps(
                {
                    "experiment": {
                        "name": "exp-json",
                        "testcase_paths": ["TC_ping.yml"],
                        "ues_paths": [],
                    },
                    "dataset": {"output": ["logs"]},
                }
            )
        ),
        headers=_headers(),
    )

    assert response.status_code == 404
