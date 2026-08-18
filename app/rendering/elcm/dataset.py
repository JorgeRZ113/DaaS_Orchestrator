"""
Generador ELCM de TestCases de entrega de datos ("dataset").

Separado a propósito del pipeline TNLCM (`app/rendering/tnlcm/*.py`). Igual que
TNLCM, se apoya en el binario `ytt` para renderizar. Los valores salen del overlay
del propio TestCase; opcionalmente se pueden inyectar data values extra (p. ej. las
métricas del dashboard leídas del TestCase de captura) vía el parámetro `data_values`.

Cada "kind" de dataset mapea a un par (template, overlay) bajo `templates/ELCM/`:
  * "csv"       -> prometheus_to_csv_dataset(.overlay).yaml
  * "dashboard" -> prometheus_to_grafana_dashboard(.overlay).yaml

El resultado es un TestCase v2 de ELCM (YAML) que en un paso posterior se subirá
al facility y se añadirá a la lista de TestCases del experimento para que ELCM
produzca de verdad el CSV / dashboard.

Resolución estricta de rutas (fail-fast):
  * Templates ÚNICAMENTE en templates/ELCM/templates/
  * Overlays ÚNICAMENTE en templates/ELCM/overlays/
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from app.core.config import settings
from app.rendering.yaml_style import _wrap_strings_in_quotes
from app.rendering.paths import templates_root_dir
from app.rendering.ytt import run_ytt_cli

logger = logging.getLogger(__name__)


# Mapa kind -> stem del par template/overlay en templates/ELCM/.
# Al añadir un nuevo formato basta con registrar aquí su stem.
ELCM_DATASET_TEMPLATES: dict[str, str] = {
    "csv": "prometheus_to_csv_dataset",
    "dashboard": "prometheus_to_grafana_dashboard",
}


def _elcm_templates_dir() -> Path:
    """Directorio canónico de templates ELCM."""
    return templates_root_dir() / "ELCM" / "templates"


def _elcm_overlays_dir() -> Path:
    """Directorio canónico de overlays ELCM."""
    return templates_root_dir() / "ELCM" / "overlays"


def resolve_dataset_assets(kind: str) -> tuple[Path, Path]:
    """
    Resolver el par (template, overlay) para un `kind` de dataset.

    Publica porque la validacion previa (`app.services.preflight`) la usa para
    comprobar que el TestCase de dataset se puede generar ANTES de desplegar la
    TN, y no al inyectarlo en mitad de la fase ELCM.

    Raises:
        ValueError: si `kind` no está declarado en ELCM_DATASET_TEMPLATES.
        FileNotFoundError: si falta el template o el overlay en disco.
    """
    stem = ELCM_DATASET_TEMPLATES.get(kind)
    if stem is None:
        raise ValueError(
            f"Unknown ELCM dataset kind '{kind}'. "
            f"Allowed: {', '.join(sorted(ELCM_DATASET_TEMPLATES))}"
        )

    template_path = _elcm_templates_dir() / f"{stem}.yaml"
    overlay_path = _elcm_overlays_dir() / f"{stem}.overlay.yaml"

    if not template_path.exists():
        raise FileNotFoundError(f"Missing ELCM dataset template: {template_path}")
    if not overlay_path.exists():
        raise FileNotFoundError(f"Missing ELCM dataset overlay: {overlay_path}")

    return template_path.resolve(), overlay_path.resolve()


def _generated_dir(execution_id: str) -> Path:
    """
    Crear y retornar artifacts/<execution_id>/archivos_generados/.

    Se alinea con el resto de generadores (TNLCM/ELCM), que escriben los pasos
    intermedios en el mismo subdirectorio de la ejecución.
    """
    base = Path(settings.artifacts_dir)
    if not base.is_absolute():
        base = Path.cwd() / base
    generated_dir = base / execution_id / "archivos_generados"
    generated_dir.mkdir(parents=True, exist_ok=True)
    return generated_dir


async def generate_elcm_dataset_testcase(
    kind: str, execution_id: str, data_values: dict[str, Any] | None = None
) -> Path:
    """
    Renderizar con ytt el TestCase de dataset `kind` y guardarlo.

    Fase 1 del generador ELCM: render puro desde el overlay (sin inyección
    externa). El subproceso `ytt` es bloqueante, así que se ejecuta fuera del
    event loop con `asyncio.to_thread` (regla §8.1).

    Args:
        kind: Formato de dataset ("csv" | "dashboard").
        execution_id: ID de la ejecución (define el directorio de salida).
        data_values: Data values #@data/values extra a inyectar en ytt (opcional);
            sobrescriben el overlay base (p. ej. measurement/metrics del dashboard).

    Returns:
        Path al TestCase renderizado, guardado como `<Name>.yml` en
        artifacts/<execution_id>/archivos_generados/ (Name = campo `Name:` del
        TestCase).

    Raises:
        ValueError: si `kind` no está soportado.
        FileNotFoundError: si falta el template o el overlay.
        RuntimeError: si ytt falla, no está disponible, o el render no tiene `Name`.
    """
    template_path, overlay_path = resolve_dataset_assets(kind)
    logger.info("[%s] Rendering ELCM dataset testcase '%s'", execution_id, kind)

    ytt_files = [template_path, overlay_path]
    if data_values:
        # Overlay #@data/values extra: sobrescribe valores del overlay base (p. ej.
        # el measurement y las metricas del dashboard, leidos del TestCase de captura).
        dv_yaml = yaml.safe_dump(data_values, sort_keys=False, allow_unicode=True)
        dv_path = _generated_dir(execution_id) / f"{kind}_datavalues.yaml"
        dv_path.write_text(f"#@data/values\n---\n{dv_yaml}", encoding="utf-8")
        ytt_files.append(dv_path)

    # §8.1: ytt es un subprocess bloqueante -> se saca del event loop.
    rendered = await asyncio.to_thread(run_ytt_cli, ytt_files)

    # ELCM referencia los TestCases V2 por su `Name:` interno, no por el nombre
    # de fichero. Guardar como <Name>.yml hace que el stem coincida con el Name.
    parsed = yaml.safe_load(rendered)
    if not isinstance(parsed, dict) or not parsed.get("Name"):
        raise RuntimeError(f"Rendered ELCM dataset testcase '{kind}' has no 'Name' field")
    testcase_name = str(parsed["Name"])

    # ELCM es muy sensible al entrecomillado y ytt emite algunos valores (las
    # queries de Prometheus, `@{ExecutionId}`, etc.) sin comillas, lo que rompe el
    # TestCase. Re-serializar forzando comillas dobles en TODOS los strings (misma
    # técnica que el descriptor TNLCM) conserva el quoting; los bool/int se dejan.
    quoted = _wrap_strings_in_quotes(parsed)
    # width alto: evita que PyYAML parta los strings largos (p. ej. la CustomQuery
    # de Flux) en varias líneas con continuación `\`; se mantienen en una sola línea.
    final_yaml = yaml.safe_dump(quoted, sort_keys=False, allow_unicode=True, width=4096)

    output_path = _generated_dir(execution_id) / f"{testcase_name}.yml"
    output_path.write_text(final_yaml, encoding="utf-8")

    logger.info(
        "[%s] ELCM dataset testcase '%s' saved: %s", execution_id, testcase_name, output_path
    )
    return output_path
