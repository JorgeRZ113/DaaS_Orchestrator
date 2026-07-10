"""
TNLCM Template Rendering & Merging: Fases 2 y 3 - YTT native rendering and fusion.

Responsabilidades:
- Fase 2: Renderizar cada template individual con su overlay relleno usando YTT nativo
- Fase 3: Fusionar todos los templates renderizados usando YTT nativo
- Guardar outputs en artifacts/<execution_id>/archivos_generados/

Resolución Estricta de Rutas:
- Templates TNLCM ÚNICAMENTE en templates/TNLCM/templates/
- Overlays TNLCM ÚNICAMENTE en templates/TNLCM/overlays/
- Nomenclatura:
  * "base" -> "base_tnlcm_descriptor.yaml" (y .overlay.yaml)
  * otros -> "{comp}_sample_tnlcm_descriptor.yaml" (y .overlay.yaml)
- Fail-fast: FileNotFoundError si no existen
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import yaml

from app import artifacts as artifacts_module
from app.models import InfrastructureConfig
from app.generators.tnlcm_overlay import (
    build_component_overlay_values,
    InvalidDataDescriptorError,
    _ensure_generated_dir,
    _save_text,
    _timer,
)
from app.utils.custom_yaml import _wrap_strings_in_quotes
from app.utils.ytt_renderer import templates_root_dir

logger = logging.getLogger(__name__)


def _run_ytt(
    ytt_files: list[Path],
    execution_id: str,
    phase_name: str = "ytt",
) -> str:
    """
    Ejecutar YTT nativo con los archivos especificados (sincrónico).

    Args:
        ytt_files: Lista de archivos para pasar a YTT (templates + overlays)
        execution_id: ID de ejecución para logging
        phase_name: Nombre de la fase para logging

    Returns:
        stdout de YTT (YAML renderizado/fusionado)

    Raises:
        RuntimeError: Si YTT falla o no está disponible
    """
    ytt_cmd = ["ytt"]
    for f in ytt_files:
        ytt_cmd.extend(["-f", str(f)])
    logger.info(f"[{execution_id}] {phase_name} YTT command: {' '.join(ytt_cmd)}")

    try:
        result = subprocess.run(
            ytt_cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        raise RuntimeError(f"YTT binary not found in PATH. Cannot proceed with {phase_name}.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"YTT {phase_name} timed out after 60 seconds.")

    if result.returncode != 0:
        error_msg = f"YTT {phase_name} failed with code {result.returncode}: {result.stderr}"
        logger.error(f"[{execution_id}] {error_msg}")
        raise RuntimeError(error_msg)

    logger.debug(f"[{execution_id}] {phase_name} completed successfully")
    return result.stdout


def _resolve_template_path_strict(comp_key: str) -> Path:
    """
    Resolver ruta estricta del template TNLCM usando nomenclatura f-string.

    Reglas:
    - Si comp_key == "base": buscar "base_tnlcm_descriptor.yaml"
    - Sino: buscar "{comp_key}_sample_tnlcm_descriptor.yaml"
    - Ubicación: templates/TNLCM/templates/

    Args:
        comp_key: Nombre del componente

    Returns:
        Path resuelto

    Raises:
        FileNotFoundError: Si el template no existe
    """
    root = templates_root_dir()
    templates_dir = root / "TNLCM" / "templates"

    if comp_key == "base":
        template_filename = "base_tnlcm_descriptor.yaml"
    else:
        template_filename = f"{comp_key}_sample_tnlcm_descriptor.yaml"

    template_path = templates_dir / template_filename

    if not template_path.exists():
        raise FileNotFoundError(
            f"Missing template: {template_filename} in {templates_dir}"
        )

    return template_path.resolve()


def _resolve_overlay_path_strict(comp_key: str) -> Path:
    """
    Resolver ruta estricta del overlay TNLCM usando nomenclatura f-string.

    Reglas:
    - Si comp_key == "base": buscar "base_tnlcm_descriptor.overlay.yaml"
    - Sino: buscar "{comp_key}_sample_tnlcm_descriptor.overlay.yaml"
    - Ubicación: templates/TNLCM/overlays/

    Args:
        comp_key: Nombre del componente

    Returns:
        Path resuelto

    Raises:
        FileNotFoundError: Si el overlay no existe
    """
    root = templates_root_dir()
    overlays_dir = root / "TNLCM" / "overlays"

    if comp_key == "base":
        overlay_filename = "base_tnlcm_descriptor.overlay.yaml"
    else:
        overlay_filename = f"{comp_key}_sample_tnlcm_descriptor.overlay.yaml"

    overlay_path = overlays_dir / overlay_filename

    if not overlay_path.exists():
        raise FileNotFoundError(
            f"Missing overlay: {overlay_filename} in {overlays_dir}"
        )

    return overlay_path.resolve()


async def render_component_template(
    comp_key: str,
    template_path: Path,
    overlay_filled_path: Path,
    execution_id: str,
) -> Path:
    """
    Fase 2: Renderizar un template individual aplicando su overlay relleno con YTT.

    Ejecuta: ytt -f {template_path} -f {overlay_filled_path}
    Guarda salida en: artifacts/<execution_id>/archivos_generados/{comp_key}_rendered.yaml

    Args:
        comp_key: Clave del componente
        template_path: Ruta al template YAML
        overlay_filled_path: Ruta al overlay relleno (#@data/values + valores)
        execution_id: ID de ejecución

    Returns:
        Path al template renderizado

    Raises:
        RuntimeError: Si YTT falla
    """
    timer = _timer(execution_id, f"render[{comp_key}]")

    # Ejecutar YTT con template + overlay
    rendered_content = _run_ytt(
        [template_path, overlay_filled_path],
        execution_id,
        phase_name=f"render[{comp_key}]",
    )

    # Guardar template renderizado
    generated_dir = _ensure_generated_dir(execution_id)
    rendered_path = generated_dir / f"{comp_key}_rendered.yaml"
    _save_text(rendered_path, rendered_content)

    logger.info(f"[{execution_id}] Component {comp_key} rendered: {rendered_path}")
    timer.stop(status="success")

    return rendered_path


async def merge_rendered_templates(
    rendered_paths: dict[str, Path],
    execution_id: str,
) -> Path:
    """
    Fase 3: Fusionar todos los templates renderizados usando YTT nativo.

    Siempre aplica formateo de comillas dobles y convierte None a strings vacíos,
    sin importar si es un solo componente o varios.

    Si solo existe "base":
        Lee base_rendered.yaml, parsea, formatea, guarda como tnlcm_descriptor.yaml
    Si múltiples componentes:
        Ejecuta: ytt -f base_rendered.yaml -f comp1_rendered.yaml -f comp2_rendered.yaml ...
        Parsea, formatea, guarda como tnlcm_descriptor.yaml

    Args:
        rendered_paths: Dict { comp_key: Path_to_rendered }
        execution_id: ID de ejecución

    Returns:
        Path a tnlcm_descriptor.yaml final con todas las comillas y sin nulls

    Raises:
        RuntimeError: Si YTT falla o solo hay componentes no-base
    """
    timer = _timer(execution_id, "merge")
    generated_dir = _ensure_generated_dir(execution_id)
    output_path = generated_dir / "tnlcm_descriptor.yaml"

    # Asegurar que "base" es el primer componente
    if "base" not in rendered_paths:
        raise RuntimeError("Base component is required for merge phase.")

    # Orden: base primero, luego el resto
    ordered_keys = ["base"] + [k for k in sorted(rendered_paths.keys()) if k != "base"]

    # Fase A: Obtener el contenido renderizado crudo
    if len(rendered_paths) == 1:
        # Solo base: leer su contenido
        merged_content = rendered_paths["base"].read_text(encoding="utf-8")
        logger.info(f"[{execution_id}] Single component (base) merge detected.")
    else:
        # Múltiples componentes: ejecutar YTT para fusión
        ytt_files = [rendered_paths[key] for key in ordered_keys]
        merged_content = _run_ytt(
            ytt_files,
            execution_id,
            phase_name="merge",
        )
        logger.info(f"[{execution_id}] Multi-component merge completed with YTT.")

    # Fase 3: FUSION FINAL Y FORMATEO DE COMILLAS / NULLS
    # Usar safe_load_all para leer todos los documentos YAML generados por YTT
    parsed_documents = list(yaml.safe_load_all(merged_content))
    
    # NUEVO: Crear un diccionario maestro donde unificaremos todo
    final_merged_dict = {"trial_network": {}}

    for doc in parsed_documents:
        if doc is not None and "trial_network" in doc:
            # Acoplar los componentes de cada documento bajo el mismo nodo raíz
            final_merged_dict["trial_network"].update(doc["trial_network"])

    # Aplicar las comillas recursivamente al diccionario fusionado
    quoted_final = _wrap_strings_in_quotes(final_merged_dict)

    # Volcar un UNICO documento, sin '---' (usando safe_dump normal)
    final_yaml = yaml.safe_dump(quoted_final, sort_keys=False, allow_unicode=True)

    # Guardar el archivo con formato final
    _save_text(output_path, final_yaml)
    logger.info(f"[{execution_id}] Final formatted TNLCM descriptor saved: {output_path}")
    timer.stop(status="success")

    return output_path


async def generate_tnlcm_descriptor(
    infra: InfrastructureConfig,
    execution_id: str,
) -> str:
    """
    Orquestación completa: Fases 1, 2 y 3 del generador TNLCM.

    Flujo:
    1. Validar que 'base' existe en components
    2. Para cada componente (base primero):
       - Fase 1: build_component_overlay_values() -> overlay relleno con cabecera @data/values
       - Fase 2: render_component_template() -> YTT individual (template + overlay relleno)
    3. Fase 3: merge_rendered_templates() -> YTT fusión final
    4. Persistir en artifacts table

    Args:
        infra: Configuración de infraestructura (contiene components)
        execution_id: ID de ejecución

    Returns:
        Ruta a artifacts/<execution_id>/archivos_generados/tnlcm_descriptor.yaml

    Raises:
        ValueError: Si 'base' no existe en components o parámetro inválido
        InvalidDataDescriptorError: Si hay campos no permitidos
        FileNotFoundError: Si no se encuentra template/overlay
        RuntimeError: Si YTT falla
    """
    timer = _timer(execution_id, "tnlcm_descriptor")
    _ensure_generated_dir(execution_id)

    components = infra.component or {}
    if not isinstance(components, dict) or "base" not in components:
        raise ValueError("'base' component is required in infrastructure.component")

    rendered_paths: dict[str, Path] = {}

    # Procesar componentes en orden: base primero
    component_order = ["base"] + [k for k in sorted(components.keys()) if k != "base"]

    for comp_key in component_order:
        comp_values = components.get(comp_key, {})
        if not isinstance(comp_values, dict):
            comp_values = {}

        logger.info(f"[{execution_id}] Processing component: {comp_key}")

        try:
            # Resolver rutas estrictas (FAIL-FAST si no existen)
            template_path = _resolve_template_path_strict(comp_key)
            overlay_path = _resolve_overlay_path_strict(comp_key)

            logger.debug(f"[{execution_id}] Resolved paths for {comp_key}:")
            logger.debug(f"  Template: {template_path}")
            logger.debug(f"  Overlay: {overlay_path}")

            # Fase 1: Rellenar overlay
            overlay_filled_path = await build_component_overlay_values(
                comp_key=comp_key,
                comp_values=comp_values,
                overlay_path=overlay_path,
                execution_id=execution_id,
            )

            # Fase 2: Renderizar template
            rendered_path = await render_component_template(
                comp_key=comp_key,
                template_path=template_path,
                overlay_filled_path=overlay_filled_path,
                execution_id=execution_id,
            )

            rendered_paths[comp_key] = rendered_path

        except FileNotFoundError as e:
            logger.error(f"[{execution_id}] File not found for component {comp_key}: {e}")
            raise
        except ValueError as e:
            logger.error(f"[{execution_id}] Invalid component or data descriptor for {comp_key}: {e}")
            raise
        except InvalidDataDescriptorError as e:
            logger.error(f"[{execution_id}] Invalid data descriptor for component {comp_key}: {e}")
            raise
        except Exception as e:
            logger.error(f"[{execution_id}] Error processing component {comp_key}: {e}")
            raise

    if not rendered_paths:
        raise RuntimeError(f"[{execution_id}] No components were successfully rendered")

    if "base" not in rendered_paths:
        raise RuntimeError(f"[{execution_id}] Base component must be rendered")

    # Fase 3: Fusionar
    final_path = await merge_rendered_templates(rendered_paths, execution_id)

    # Persistir en tabla de artefactos
    try:
        artifacts_module.persist_generated_artifacts(
            execution_id, tnlcm_descriptor_path=str(final_path)
        )
    except Exception as e:
        logger.warning(f"[{execution_id}] Could not persist artifacts: {e}")

    logger.info(f"[{execution_id}] TNLCM descriptor generation completed: {final_path}")
    timer.stop(status="success")

    return str(final_path)

