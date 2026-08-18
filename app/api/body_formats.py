"""Las tres vias de entrada del descriptor: body JSON, body YAML y fichero subido.

El anteproyecto compromete que el usuario describa su experimento en un unico
fichero YAML. Como `DatasetDescriptor` es un modelo Pydantic -- y Pydantic valida
estructuras en memoria, no formatos de texto -- basta con decodificar la entrada
hacia el mismo modelo para cumplirlo, sin tocar ni la validacion ni el resto del
pipeline.

La conversion se hace ANTES de que FastAPI parsee el body, reescribiendo la
peticion como si hubiera llegado en JSON. Asi el endpoint mantiene su firma
(`descriptor: DatasetDescriptor`) y con ella el esquema OpenAPI, los mensajes 422
y el `exclude_unset` del que depende `reject_empty_strings_or_raise`. La
alternativa -- recibir un `Request` crudo y parsear a mano -- borraria el modelo
de Swagger, que es justo el artefacto que documenta el contrato.
"""

import json
import logging
import re
from typing import Any, Callable

import yaml
from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute

from app.domain.descriptor import DescriptorSource

logger = logging.getLogger(__name__)


JSON_MEDIA_TYPE = "application/json"
YAML_MEDIA_TYPES = frozenset({"application/yaml", "application/x-yaml", "text/yaml", "text/x-yaml"})
MULTIPART_MEDIA_TYPE = "multipart/form-data"

# Nombre del campo del formulario que transporta el fichero, igual que el
# `descriptor` del endpoint de TNLCM para no inventar un vocabulario distinto.
DESCRIPTOR_FIELD = "descriptor"

# Tope del cuerpo de la peticion. `yaml.safe_load` impide construir objetos
# arbitrarios, pero no protege de la bomba de expansion por anclas y alias
# (billion laughs): un documento diminuto puede expandirse hasta agotar memoria.
# El tope se comprueba sobre los bytes recibidos, antes de parsear nada.
MAX_BODY_BYTES = 1024 * 1024


class JsonLikeLoader(yaml.SafeLoader):
    """SafeLoader con el esquema core de YAML 1.2: los mismos tipos que JSON.

    PyYAML implementa YAML 1.1, que resuelve `no`, `off`, `yes` y `on` como
    booleanos, `22:30` como el sexagesimal 1350 y `007` como 7. Eso importa
    porque los valores de `infrastructure.component.*` no estan tipados
    (`Dict[str, Any]`) y viajan tal cual, via `tnlcm_data_values()`, hasta el
    overlay que consume ytt: alguien que escribe `deploy: no` obtendria `False`
    donde el mismo descriptor en JSON habria dejado la cadena `"no"`.

    Con el esquema core de 1.2 las dos codificaciones producen exactamente la
    misma estructura, que es lo que permite afirmar que el formato de transporte
    es indiferente.

    Cuidado al mantener esto: `add_implicit_resolver` APILA sobre los resolvers
    heredados en vez de sustituirlos, asi que hay que vaciar la tabla primero o
    los de YAML 1.1 seguirian ganando por orden de registro.
    """


JsonLikeLoader.yaml_implicit_resolvers = {}

for _tag, _pattern, _first_chars in (
    ("tag:yaml.org,2002:bool", r"^(?:true|True|TRUE|false|False|FALSE)$", "tTfF"),
    ("tag:yaml.org,2002:int", r"^-?(?:0|[1-9][0-9]*)$", "-0123456789"),
    (
        "tag:yaml.org,2002:float",
        r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]*)?(?:[eE][-+]?[0-9]+)?$",
        "-0123456789",
    ),
    ("tag:yaml.org,2002:null", r"^(?:null|Null|NULL|~|)$", "nN~\0"),
):
    JsonLikeLoader.add_implicit_resolver(_tag, re.compile(_pattern), list(_first_chars))


def _media_type(request: Request) -> str:
    """Tipo de contenido sin parametros (`; charset=`, `; boundary=`)."""
    return request.headers.get("content-type", "").split(";")[0].strip().lower()


def parse_yaml_descriptor(text: str) -> dict[str, Any]:
    """Convierte el texto YAML en el mapping que espera Pydantic.

    Traduce los fallos a un 400 accionable en vez de dejar que exploten como 500.
    El error de sintaxis lleva linea y columna, que no tiene equivalente en el
    camino JSON: es la ventaja de UX de aceptar ficheros escritos a mano.
    """
    try:
        data = yaml.load(text, Loader=JsonLikeLoader)
    except yaml.YAMLError as exc:
        detail: dict[str, Any] = {
            "yaml_error": getattr(exc, "problem", None) or str(exc),
            "message": "El descriptor no es YAML valido. Corrige la sintaxis y reenvia.",
        }
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            # `problem_mark` cuenta desde 0; los editores desde 1.
            detail["line"] = mark.line + 1
            detail["column"] = mark.column + 1
        if isinstance(exc, yaml.composer.ComposerError):
            detail["message"] = (
                "El descriptor debe ser un unico documento YAML (sin separadores '---')."
            )
        raise HTTPException(status_code=400, detail=detail) from exc

    if data is None:
        raise HTTPException(
            status_code=400,
            detail={
                "yaml_error": "empty document",
                "message": "El descriptor esta vacio.",
            },
        )

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "yaml_error": f"root is {type(data).__name__}, expected mapping",
                "message": (
                    "El descriptor debe ser un mapping YAML con las claves de primer "
                    "nivel (infrastructure, experiment, dataset...)."
                ),
            },
        )

    return data


def _reject_oversized(size: int) -> None:
    if size > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "received_bytes": size,
                "max_bytes": MAX_BODY_BYTES,
                "message": "El descriptor supera el tamano maximo admitido.",
            },
        )


async def _read_multipart_descriptor(request: Request) -> str:
    """Extrae el contenido del fichero subido en el campo `descriptor`."""
    form = await request.form()
    upload = form.get(DESCRIPTOR_FIELD)

    if upload is None:
        raise HTTPException(
            status_code=400,
            detail={
                "missing_field": DESCRIPTOR_FIELD,
                "message": (
                    f"La peticion multipart debe incluir el fichero en el campo "
                    f"'{DESCRIPTOR_FIELD}'."
                ),
            },
        )

    if isinstance(upload, str):
        # El campo llego como texto plano en vez de como fichero: sirve igual.
        raw_bytes = upload.encode("utf-8")
    else:
        raw_bytes = await upload.read()

    _reject_oversized(len(raw_bytes))
    return _decode(raw_bytes)


def _decode(raw_bytes: bytes) -> str:
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "yaml_error": "body is not valid UTF-8",
                "message": "El descriptor debe estar codificado en UTF-8.",
            },
        ) from exc


def _as_json_request(request: Request, payload: dict[str, Any]) -> Request:
    """Rehace la peticion como si hubiera llegado en JSON.

    Se construye un `Request` nuevo sobre el mismo scope en vez de mutar
    `request._body`: el atributo es privado de Starlette y su semantica ha ido
    cambiando entre versiones, mientras que pasar un `receive` propio es contrato
    ASGI estable.
    """
    body = json.dumps(payload).encode("utf-8")

    scope = dict(request.scope)
    headers = []
    for key, value in request.scope["headers"]:
        if key == b"content-type":
            headers.append((key, JSON_MEDIA_TYPE.encode()))
        elif key == b"content-length":
            headers.append((key, str(len(body)).encode()))
        else:
            headers.append((key, value))
    scope["headers"] = headers

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class MultiFormatRoute(APIRoute):
    """Ruta que admite el cuerpo en JSON, en YAML o como fichero YAML subido.

    El camino JSON no se toca en absoluto: se delega sin leer el body, para que
    los clientes existentes se comporten exactamente igual que antes. Las otras
    dos vias se normalizan a JSON y siguen el mismo recorrido.

    El origen queda en `request.state.descriptor_source` para que el endpoint lo
    pase al orquestador y `storage` pueda persistir el texto original verbatim.
    """

    def get_route_handler(self) -> Callable:
        original_route_handler = super().get_route_handler()

        async def multi_format_route_handler(request: Request) -> Response:
            media_type = _media_type(request)

            if media_type in YAML_MEDIA_TYPES:
                raw_bytes = await request.body()
                _reject_oversized(len(raw_bytes))
                raw_text = _decode(raw_bytes)
            elif media_type == MULTIPART_MEDIA_TYPE:
                raw_text = await _read_multipart_descriptor(request)
            else:
                request.state.descriptor_source = DescriptorSource(format="json")
                return await original_route_handler(request)

            payload = parse_yaml_descriptor(raw_text)
            logger.debug(
                "Descriptor recibido como %s (%d bytes)", media_type, len(raw_text.encode())
            )

            json_request = _as_json_request(request, payload)
            json_request.state.descriptor_source = DescriptorSource(format="yaml", raw=raw_text)
            return await original_route_handler(json_request)

        return multi_format_route_handler


def descriptor_source_of(request: Request) -> DescriptorSource:
    """Origen del descriptor de esta peticion, con JSON como valor por defecto.

    El default cubre a quien monte el router sin `MultiFormatRoute` (por ejemplo
    un test que instancie el endpoint por su cuenta) sin obligarle a preparar el
    `state`.
    """
    return getattr(request.state, "descriptor_source", None) or DescriptorSource(format="json")


def yaml_request_body(schema_ref: str, description: str) -> dict[str, Any]:
    """`openapi_extra` que anuncia las dos vias YAML junto a la de JSON.

    FastAPI fusiona este fragmento con el `requestBody` que ya genera a partir de
    la firma, de modo que el `$ref` del modelo se mantiene y solo se anaden
    formatos. El `format: binary` del multipart es lo que hace que Swagger UI
    pinte un selector de fichero en vez de un area de texto.
    """
    return {
        "requestBody": {
            "content": {
                "application/yaml": {"schema": {"$ref": f"#/components/schemas/{schema_ref}"}},
                MULTIPART_MEDIA_TYPE: {
                    "schema": {
                        "type": "object",
                        "required": [DESCRIPTOR_FIELD],
                        "properties": {
                            DESCRIPTOR_FIELD: {
                                "type": "string",
                                "format": "binary",
                                "description": description,
                            }
                        },
                    }
                },
            }
        }
    }
