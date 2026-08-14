"""Alta de usuario en TNLCM y refresco del token.

El token vive en globales de modulo del adaptador (deuda conocida: §9 Fase 2 del
roadmap), asi que estos endpoints lo escriben ahi. Ninguna respuesta devuelve el
token entero: solo una vista previa enmascarada.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from app.adapters import tnlcm
from app.adapters.http import response_error_detail
from app.adapters.tnlcm_schemas import RegisterRequest, TokenPair
from app.api.deps import verify_api_key
from app.core.config import settings
from app.observability.telemetry import telemetry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


def _masked(token: str) -> str:
    return f"{token[:12]}...{token[-6:]}" if len(token) > 20 else "[token-set]"


@router.post("/register")
async def post_register(
    username: str = Query(..., description="username, required"),
    password: str = Query(..., description="password, required"),
    org: str | None = Query(None, description="org, optional"),
    email: str | None = Query(None, description="email, optional"),
) -> dict[str, str]:
    """Registro que delega en TNLCM y luego realiza login para obtener token.

    Todos los parámetros se reciben por query string: ?username=x&password=y&email=a&org=b
    - `username` y `password` son obligatorios.
    - `email` y `org` son opcionales.

    Luego realiza login para obtener los tokens, los guarda en memoria y devuelve
    una respuesta con token enmascarado.
    """
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")

    # TNLCM espera que las claves opcionales se omitan, no que lleguen a null.
    register_payload: dict = RegisterRequest(
        username=username, password=password, email=email, org=org
    ).model_dump(exclude_none=True)

    telemetry.increment_counter(
        "requests_total", labels={"service": "auth", "operation": "register"}
    )
    telemetry.log_event("info", "user.register.request", username=username, email=email)

    async with httpx.AsyncClient(timeout=tnlcm.TNLCM_LOGIN_TIMEOUT_SECONDS) as client:
        # 1) Registrar en TNLCM (no auth)
        try:
            resp = await client.post(
                f"{settings.tnlcm_url}/api/v1/user/register",
                json=register_payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response_error_detail(exc.response) or "unknown"
            telemetry.increment_counter(
                "errors_total",
                labels={
                    "service": "auth",
                    "operation": "register",
                    "error_type": str(exc.response.status_code),
                },
            )
            raise HTTPException(
                status_code=exc.response.status_code, detail=f"TNLCM register failed: {detail}"
            ) from exc
        except httpx.TimeoutException as exc:
            telemetry.increment_counter(
                "errors_total", labels={"service": "auth", "operation": "register"}
            )
            raise HTTPException(
                status_code=504, detail="Timeout contacting TNLCM register endpoint"
            ) from exc

        # 2) Login con las credenciales recien creadas para obtener los tokens
        try:
            login_resp = await client.post(
                f"{settings.tnlcm_url}/api/v1/user/login",
                auth=(username, password),
                headers={"Accept": "application/json"},
            )
            login_resp.raise_for_status()
            response_data = login_resp.json()
        except httpx.HTTPStatusError as exc:
            detail = response_error_detail(exc.response) or "unknown"
            telemetry.increment_counter(
                "errors_total",
                labels={
                    "service": "auth",
                    "operation": "login",
                    "error_type": str(exc.response.status_code),
                },
            )
            raise HTTPException(
                status_code=502, detail=f"TNLCM login failed after register: {detail}"
            ) from exc
        except httpx.TimeoutException as exc:
            telemetry.increment_counter(
                "errors_total", labels={"service": "auth", "operation": "login"}
            )
            raise HTTPException(
                status_code=504, detail="Timeout contacting TNLCM login endpoint"
            ) from exc

    try:
        tokens = TokenPair.from_login_response(response_data)
    except ValueError as exc:
        # El detalle no lleva el body: contiene los tokens.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    tnlcm._tnlcm_access_token = tokens.access_token
    if tokens.refresh_token:
        tnlcm._tnlcm_refresh_token = tokens.refresh_token

    telemetry.log_event("info", "user.register.completed", username=username)

    return {"status": "ok", "token_preview": _masked(tnlcm._tnlcm_access_token)}


@router.post("/login", dependencies=[Depends(verify_api_key)])
def refresh_tnlcm_token():
    """Genera token TNLCM con user/password de .env y lo guarda en memoria."""
    try:
        token = tnlcm.login_tnlcm_and_persist_token()
        logger.info("TNLCM token refreshed and successfully logged in")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail=(
                "TNLCM login timeout after 20 seconds. "
                "Verifica la conectividad y que la VPN este activa."
            ),
        )
    except httpx.HTTPStatusError as exc:
        upstream_detail = ""
        try:
            payload = exc.response.json()
            if isinstance(payload, dict):
                upstream_detail = str(
                    payload.get("message")
                    or payload.get("detail")
                    or payload.get("error")
                    or payload
                )
            else:
                upstream_detail = str(payload)
        except Exception:
            upstream_detail = (exc.response.text or "").strip()

        detail = (
            f"TNLCM login failed: HTTP {exc.response.status_code}. "
            f"Backend error: {upstream_detail or 'unknown'}"
        )
        raise HTTPException(status_code=502, detail=detail)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not refresh TNLCM token: {exc}")

    # Solo una vista previa segura, nunca el token entero.
    return {
        "message": "TNLCM token refreshed and stored in memory",
        "token_preview": _masked(token),
    }
