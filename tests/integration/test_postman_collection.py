"""La coleccion Postman tiene que seguir siendo ejecutable.

`API_JSON/DaaS.postman_collection.json` es la forma en que se demuestra la API en
vivo, incluida la defensa. Una peticion que devuelve 400 en cuanto se pulsa Send
es peor que no tenerla, y el fallo no se ve hasta que alguien la lanza: cuando se
escribio esta prueba, dos peticiones referenciaban un TestCase inexistente
(`TestCase_ping.yml`) y otra pedia modos de dataset sin el TestCase de captura que
exigen.
"""

import json
from pathlib import Path

import pytest
import yaml

from app.api.body_formats import JsonLikeLoader
from app.api.schemas.requests import ElcmExperimentRequest
from app.domain.descriptor import DatasetDescriptor
from app.services import preflight

COLLECTION = Path(__file__).resolve().parents[2] / "API_JSON" / "DaaS.postman_collection.json"


def _collection() -> dict:
    return json.loads(COLLECTION.read_text(encoding="utf-8"))


def _post_requests_with_body() -> list[tuple[str, dict]]:
    """Peticiones POST con cuerpo literal (las multipart no lo llevan)."""
    found: list[tuple[str, dict]] = []
    for group in _collection()["item"]:
        for item in group.get("item", []):
            request = item["request"]
            body = request.get("body") or {}
            if request["method"] == "POST" and body.get("raw"):
                found.append((item["name"], request))
    return found


def _content_type(request: dict) -> str:
    for header in request.get("header", []):
        if header["key"].lower() == "content-type":
            return header["value"]
    return ""


def _decode(request: dict):
    raw = request["body"]["raw"]
    if "json" in _content_type(request):
        return json.loads(raw)
    return yaml.load(raw, Loader=JsonLikeLoader)


CASES = _post_requests_with_body()
IDS = [name for name, _ in CASES]


@pytest.mark.parametrize("name,request_data", CASES, ids=IDS)
def test_request_body_validates(name: str, request_data: dict) -> None:
    model = ElcmExperimentRequest if "/elcm" in name else DatasetDescriptor
    model.model_validate(_decode(request_data))


@pytest.mark.parametrize("name,request_data", CASES, ids=IDS)
def test_request_passes_the_elcm_preflight(name: str, request_data: dict) -> None:
    """Los TestCases y UEs referenciados existen y cumplen lo que exige ELCM."""
    model = ElcmExperimentRequest if "/elcm" in name else DatasetDescriptor
    parsed = model.model_validate(_decode(request_data))

    preflight.validate_elcm_request(parsed.experiment, parsed.dataset)


def test_every_input_format_is_demonstrated() -> None:
    """La coleccion debe ensenar las tres vias, no solo la de JSON."""
    modes = set()
    for group in _collection()["item"]:
        for item in group.get("item", []):
            request = item["request"]
            body = request.get("body") or {}
            if request["method"] != "POST" or not body:
                continue
            if body.get("mode") == "formdata":
                modes.add("multipart/form-data")
            elif "yaml" in _content_type(request):
                modes.add("application/yaml")
            elif "json" in _content_type(request):
                modes.add("application/json")

    assert modes == {"application/json", "application/yaml", "multipart/form-data"}


def test_multipart_request_uses_the_expected_field() -> None:
    """El campo tiene que llamarse `descriptor`; con otro nombre la API da 400."""
    uploads = [
        field
        for group in _collection()["item"]
        for item in group.get("item", [])
        for field in (item["request"].get("body") or {}).get("formdata", [])
        if field.get("type") == "file"
    ]

    assert uploads, "no hay ninguna peticion que suba el descriptor como fichero"
    assert all(field["key"] == "descriptor" for field in uploads)
