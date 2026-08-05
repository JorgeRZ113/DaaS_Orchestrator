"""Cliente HTTP fino para la API del DaaS Orchestrator.

Centraliza las llamadas a los endpoints existentes para que la UI de Streamlit
no repita la construcción de peticiones ni el manejo de errores. Cada método
devuelve el cuerpo JSON ya deserializado en caso de éxito, o levanta `ApiError`
con un mensaje legible (y el `detail` original disponible para pintarlo).

Es un cliente SÍNCRONO a propósito: Streamlit ejecuta en un proceso aparte y sin
event loop, así que usar `httpx` en modo bloqueante es lo correcto aquí.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

# Timeout por defecto para operaciones ligeras (estado, health, login).
DEFAULT_TIMEOUT_SECONDS = 30.0
# El despliegue TNLCM puede tardar; se le da un margen mayor.
DEPLOY_TIMEOUT_SECONDS = 120.0

DATASET_OUTPUTS: tuple[str, ...] = ("logs", "csv", "dashboard", "raw")


class ApiError(Exception):
    """Error de comunicación con la API ya traducido a mensaje legible.

    Attributes:
        message: Texto listo para mostrar al usuario.
        status_code: Código HTTP devuelto (None si ni siquiera hubo respuesta).
        detail: Cuerpo `detail` original (str o dict), útil para listar campos.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


def _extract_detail(response: httpx.Response) -> Any:
    """Devuelve el campo `detail` de una respuesta de error de FastAPI.

    FastAPI encapsula los errores en `{"detail": ...}`, donde `detail` puede ser
    un string o un dict (p. ej. `{"empty_fields": [...]}`). Si el cuerpo no es
    JSON, se devuelve el texto crudo.
    """
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()
    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    return payload


def _format_error(status_code: int, detail: Any) -> str:
    """Traduce (código, detail) a un mensaje corto y legible para la UI."""
    if isinstance(detail, dict):
        if "empty_fields" in detail:
            fields = ", ".join(detail.get("empty_fields", []))
            return f"Campos vacíos que debes rellenar o quitar: {fields}"
        if "invalid_fields" in detail:
            fields = ", ".join(detail.get("invalid_fields", []))
            return f"Campos inválidos: {fields}"
        message = detail.get("message") or detail.get("detail")
        if message:
            return str(message)
        return f"HTTP {status_code}: {detail}"

    text = str(detail).strip() if detail else ""
    if status_code == 401:
        return "API key inválida (revisa la clave en el panel lateral)."
    if status_code == 404:
        return text or "Recurso no encontrado."
    if status_code == 409:
        return text or "Conflicto: hay una operación en curso o ya realizada."
    if status_code == 504:
        return text or "Timeout contactando con un servicio dependiente."
    return text or f"Error HTTP {status_code}."


def _parse_response(response: httpx.Response) -> Any:
    """Devuelve el JSON en caso de éxito; levanta `ApiError` en caso contrario."""
    if response.is_success:
        try:
            return response.json()
        except ValueError:
            return (response.text or "").strip()

    detail = _extract_detail(response)
    raise ApiError(
        _format_error(response.status_code, detail),
        status_code=response.status_code,
        detail=detail,
    )


@dataclass
class ApiClient:
    """Wrapper de una sola instancia de conexión (base URL + API key)."""

    base_url: str
    api_key: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def _headers(self, *, auth: bool) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if auth:
            headers["X-API-Key"] = self.api_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Ejecuta la petición y normaliza errores de red y de estado HTTP."""
        url = f"{self.base_url.rstrip('/')}{path}"
        try:
            response = httpx.request(
                method,
                url,
                headers=self._headers(auth=auth),
                json=json,
                params=params,
                timeout=timeout or self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise ApiError(f"Timeout al contactar {url}") from exc
        except httpx.HTTPError as exc:
            raise ApiError(f"No se pudo conectar con {url}: {exc}") from exc
        return _parse_response(response)

    # ----- Health -----

    def health_services(self) -> Any:
        """Liveness del orquestador y de TNLCM (no requiere API key)."""
        return self._request("GET", "/health/services", auth=False)

    def health_components(self) -> Any:
        """Salud HTTP de InfluxDB/Grafana/Prometheus/ELCM (requiere API key)."""
        return self._request("GET", "/health/components", auth=True)

    # ----- Auth TNLCM -----

    def login_tnlcm(self) -> Any:
        """Refresca el token TNLCM usando las credenciales del .env del servidor."""
        return self._request("POST", "/login", auth=True)

    def register_tnlcm(
        self,
        username: str,
        password: str,
        email: str | None = None,
        org: str | None = None,
    ) -> Any:
        """Registra un usuario en TNLCM (los datos van por query string)."""
        params: dict[str, Any] = {"username": username, "password": password}
        if email:
            params["email"] = email
        if org:
            params["org"] = org
        return self._request("POST", "/register", auth=False, params=params)

    # ----- Ejecuciones -----

    def create_execution(self, body: dict[str, Any]) -> Any:
        """Lanza una ejecución completa (TNLCM y, opcionalmente, ELCM)."""
        return self._request(
            "POST",
            "/executions",
            auth=True,
            json=body,
            timeout=DEPLOY_TIMEOUT_SECONDS,
        )

    def get_execution(self, execution_id: str) -> Any:
        """Estado resumido de una ejecución."""
        return self._request("GET", f"/executions/{execution_id}", auth=True)

    def get_execution_detail(self, execution_id: str) -> Any:
        """Registro completo de una ejecución (artifacts, experimentos, error)."""
        return self._request("GET", f"/executions/{execution_id}/detail", auth=True)

    def get_execution_summary(self, execution_id: str, *, as_markdown: bool = False) -> Any:
        """Resumen legible: pasos, duraciones, resultados y errores explicados.

        El backend lo construye en vivo, así que puede consultarse mientras la
        ejecución sigue en curso. Con `as_markdown=True` devuelve el texto del
        informe (el mismo `summary.md` que se guarda en `artifacts/`) en lugar
        del JSON; `_parse_response` ya se encarga de devolverlo como str.
        """
        params = {"format": "markdown"} if as_markdown else None
        return self._request(
            "GET",
            f"/executions/{execution_id}/summary",
            auth=True,
            params=params,
        )

    def start_elcm(self, execution_id: str, body: dict[str, Any]) -> Any:
        """Lanza un experimento ELCM sobre la TN viva de la ejecución."""
        return self._request(
            "POST",
            f"/executions/{execution_id}/elcm",
            auth=True,
            json=body,
            timeout=DEPLOY_TIMEOUT_SECONDS,
        )

    def delete_tn(self, execution_id: str) -> Any:
        """Dispara el borrado de la Trial Network de la ejecución."""
        return self._request("DELETE", f"/executions/{execution_id}/tn", auth=True)
