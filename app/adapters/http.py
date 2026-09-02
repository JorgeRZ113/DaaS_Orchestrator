"""Utilidades comunes a los adaptadores HTTP.

Estas cuatro funciones estaban duplicadas **byte a byte** entre
`adapters/tnlcm.py` y `adapters/elcm.py`. No son especificas de ningun backend:
registrar una respuesta y sacarle el mensaje de error es lo mismo para TNLCM que
para ELCM. La resolucion contra `examples/` la usa ya solo TNLCM (el descriptor
de infraestructura); los TestCases y UEs de ELCM se resuelven contra la
biblioteca `templates/ELCM/TestCase/` (`rendering/paths.elcm_testcase_dir`).

Son publicas a proposito. Antes llevaban prefijo `_` pero se importaban desde
otros modulos, con lo que la convencion mentia: un nombre privado que cruza la
frontera del modulo no es privado.
"""

import json
import logging
from pathlib import Path

import httpx

from app.core.config import settings

# Cuerpo que se registra de una respuesta antes de recortarlo.
MAX_LOGGED_BODY_CHARS = 500


def log_http_response(service: str, response: httpx.Response) -> None:
    """Registra metodo, URL, codigo y un extracto del cuerpo de la respuesta.

    El evento se emite en el logger del adaptador que hizo la llamada
    (`app.adapters.tnlcm`, `app.adapters.elcm`) y no en el de este modulo: quien
    filtre sus logs por adaptador sigue viendo exactamente lo mismo que antes de
    unificar las dos copias.
    """
    logger = logging.getLogger(f"app.adapters.{service.lower()}")

    body = ""
    if hasattr(response, "text"):
        text_value = getattr(response, "text")
        body = text_value if text_value is not None else ""
    elif hasattr(response, "json"):
        try:
            body = json.dumps(response.json())
        except Exception:
            body = ""
    body = body.replace("\n", " ").strip()
    if len(body) > MAX_LOGGED_BODY_CHARS:
        body = f"{body[:MAX_LOGGED_BODY_CHARS]}..."

    request = getattr(response, "request", None)
    logger.info(
        "%s %s %s -> %s | %s",
        service,
        getattr(request, "method", "?"),
        getattr(request, "url", "?"),
        getattr(response, "status_code", "?"),
        body,
    )


def response_error_detail(response: httpx.Response | None) -> str:
    """Extrae el mensaje de error de una respuesta, mire donde lo mire el backend.

    TNLCM y ELCM colocan el detalle en claves distintas segun el endpoint, asi
    que se prueban por orden y, si ninguna aparece, se devuelve el cuerpo crudo.
    """
    if response is None:
        return ""

    try:
        payload = response.json()
        if isinstance(payload, dict):
            for key in ("message", "detail", "error", "errors"):
                value = payload.get(key)
                if value is None:
                    continue
                if isinstance(value, (dict, list)):
                    return json.dumps(value)
                return str(value)
            return json.dumps(payload)
        if isinstance(payload, list):
            return json.dumps(payload)
        if isinstance(payload, str):
            return payload
    except Exception:
        pass

    return (response.text or "").strip()


def examples_base_dir() -> Path:
    """Directorio `examples/` resuelto a ruta absoluta."""
    base = Path(settings.examples_dir)
    if not base.is_absolute():
        base = Path.cwd() / base
    return base.resolve()


def resolve_examples_path(path_or_name: str | None) -> str | None:
    """Resuelve un nombre de fichero contra `examples/`; las rutas absolutas pasan tal cual."""
    if not path_or_name:
        return None

    candidate = Path(path_or_name)
    if candidate.is_absolute():
        return str(candidate)

    return str((examples_base_dir() / candidate).resolve())
