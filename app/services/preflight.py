"""Validacion previa (pre-flight) de la parte ELCM de un DatasetDescriptor.

La fase ELCM corre en segundo plano y sobre una TN ya desplegada: cualquier
error del body que solo se detecte alli (un fichero mal escrito, un TestCase sin
`Version: 2`, un UE con sintaxis de TestCase, un dashboard sin TestCase de
captura) llega despues de haber levantado toda la infraestructura, deja un
experimento FAILED y quema su nombre para el resto de la vida de la TN.

Este modulo adelanta al endpoint todo lo que se puede comprobar sin tocar la TN,
de modo que el error sea un 400 con la lista COMPLETA de problemas y baste con
corregir el body y reenviar. Lo que solo se sabe con la TN viva (que ELCM acepte
el descriptor, que el experimento termine) sigue resolviendose en la fase.

Las reglas replicadas aqui son las del motor ELCM, las mismas que `tests/
contract/test_testcase_library_contract.py` vigila en CI para los ficheros de
`templates/ELCM/TestCase/`: este modulo las aplica ademas a los ficheros que
llegan por el body y que nunca pasan por CI.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from app.adapters import elcm
from app.services.phases import elcm as elcm_phase
from app.rendering.elcm import dataset as elcm_dataset
from app.domain.descriptor import DatasetRequest, ExperimentConfig
from app.rendering.paths import elcm_testcase_dir, resolve_template_path

logger = logging.getLogger(__name__)

# Plantilla base del Experiment Descriptor de ELCM: sin ella la fase no puede
# construir la peticion de /experiment/run.
EXPERIMENT_DESCRIPTOR_TEMPLATE = "ELCM/template_experiment_descriptor.json"


def _testcase_library_label() -> str:
    """Nombra la biblioteca por su ruta en el repositorio, no por la absoluta.

    El mensaje viaja en un 400 al cliente: `templates/ELCM/TestCase` es lo que el
    usuario reconoce, y no expone el layout del servidor.
    """
    library = elcm_testcase_dir()
    return library.relative_to(library.parents[2]).as_posix()


class ElcmPreflightError(ValueError):
    """Agrupa TODOS los problemas encontrados en la parte ELCM del descriptor.

    Se acumulan en lugar de abortar en el primero para que una sola respuesta
    diga todo lo que hay que corregir antes de reenviar el body.
    """

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _safe_load_mapping(path: Path, label: str, problems: list[str]) -> dict[str, Any] | None:
    """Carga un YAML referenciado por el body y exige que sea un mapping."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        problems.append(f"{label}: is not readable YAML ({exc.__class__.__name__})")
        return None

    if not isinstance(data, dict) or not data:
        problems.append(f"{label}: must be a non-empty YAML mapping")
        return None
    return data


def _validate_testcases(experiment: ExperimentConfig, problems: list[str]) -> list[str]:
    """Resuelve los TestCases del body y comprueba el contrato que exige ELCM.

    Devuelve las rutas ya resueltas de los TestCases validos, que la validacion
    del dataset reutiliza para localizar el TestCase de captura.
    """
    if not experiment.testcase_paths:
        problems.append("experiment.testcase_paths: at least one TestCase is required")

    resolved: list[str] = []
    seen_names: dict[str, str] = {}

    for testcase_ref in experiment.testcase_paths:
        label = f"experiment.testcase_paths '{testcase_ref}'"
        try:
            path = elcm.resolve_testcase_file(testcase_ref)
        except FileNotFoundError:
            problems.append(f"{label}: file not found in '{_testcase_library_label()}'")
            continue

        data = _safe_load_mapping(path, label, problems)
        if data is None:
            continue

        # ELCM registra los TestCases V2 por su `Name:` interno y su endpoint de
        # subida rechaza `Name` sin `Version: 2` (y viceversa): sin ambos el
        # fichero se sube pero el Facility nunca lo registra, y el experimento
        # arranca sin ese TestCase.
        name = data.get("Name")
        if not name:
            problems.append(
                f"{label}: has no 'Name' (ELCM registers TestCases by their internal Name)"
            )
        if data.get("Version") != 2:
            problems.append(
                f"{label}: has no 'Version: 2' (ELCM rejects a TestCase with 'Name' "
                f"but no 'Version: 2')"
            )

        if name:
            duplicate_of = seen_names.get(str(name))
            if duplicate_of is not None:
                problems.append(
                    f"{label}: declares the same Name '{name}' as '{duplicate_of}'; "
                    f"ELCM keeps only one TestCase per Name"
                )
            else:
                seen_names[str(name)] = testcase_ref

        resolved.append(str(path))

    return resolved


def _validate_ues(experiment: ExperimentConfig, problems: list[str]) -> None:
    """Resuelve los UEs del body y comprueba su estructura V1."""
    for ue_ref in experiment.ues_paths:
        # Mismo criterio que la fase ELCM, que ignora las referencias vacias.
        if not ue_ref:
            continue

        label = f"experiment.ues_paths '{ue_ref}'"
        try:
            path = elcm.resolve_ue_file(ue_ref)
        except FileNotFoundError:
            problems.append(f"{label}: file not found in '{_testcase_library_label()}'")
            continue

        # La fase ya valida la estructura del UE al generar el descriptor: se
        # reutiliza su validador para aceptar aqui exactamente lo mismo.
        try:
            elcm.extract_ue_name(str(path))
        except (OSError, yaml.YAMLError) as exc:
            problems.append(f"{label}: is not readable YAML ({exc.__class__.__name__})")
        except ValueError as exc:
            problems.append(f"{label}: {exc}")


def _validate_dataset(
    dataset: DatasetRequest, testcase_files: list[str], problems: list[str]
) -> None:
    """Comprueba que la entrega de datos pedida se puede producir."""
    unsupported = [
        fmt for fmt in dataset.output if fmt not in elcm_phase.IMPLEMENTED_DATASET_OUTPUTS
    ]
    if unsupported:
        problems.append(
            f"dataset.output '{', '.join(unsupported)}': not implemented yet. Currently "
            f"supported: {', '.join(sorted(elcm_phase.IMPLEMENTED_DATASET_OUTPUTS))}"
        )

    # csv y dashboard inyectan un TestCase generado con ytt: si falta su par
    # template/overlay la fase aborta con la TN ya desplegada.
    for kind in ("csv", "dashboard"):
        if not dataset.wants(kind) or kind not in elcm_dataset.ELCM_DATASET_TEMPLATES:
            continue
        try:
            elcm_dataset.resolve_dataset_assets(kind)
        except (FileNotFoundError, ValueError) as exc:
            problems.append(f"dataset.output '{kind}': {exc}")

    # El dashboard se pinta con un panel por metrica, y las metricas solo pueden
    # salir del TestCase de captura: sin el no hay nada que representar (mismo
    # criterio que `_dataset_data_values` en la fase).
    if dataset.wants("dashboard") and elcm.extract_capture_metrics(testcase_files) is None:
        problems.append(
            "dataset.output 'dashboard': requires a capture TestCase (*_capture* with "
            "Run.PrometheusToInflux and plain metric names) in experiment.testcase_paths"
        )


def validate_elcm_request(experiment: ExperimentConfig, dataset: DatasetRequest) -> None:
    """Valida la parte ELCM de la peticion antes de aceptarla (Fail-Fast).

    Args:
        experiment: Experimento pedido (TestCases y UEs a ejecutar).
        dataset: Entrega de datos pedida para ese experimento.

    Raises:
        ElcmPreflightError: con la lista completa de problemas encontrados.
    """
    problems: list[str] = []

    testcase_files = _validate_testcases(experiment, problems)
    _validate_ues(experiment, problems)
    _validate_dataset(dataset, testcase_files, problems)

    if resolve_template_path(EXPERIMENT_DESCRIPTOR_TEMPLATE, category="ELCM") is None:
        problems.append(
            f"Experiment descriptor template not found: {EXPERIMENT_DESCRIPTOR_TEMPLATE}"
        )

    if problems:
        logger.info(
            "ELCM preflight rejected experiment '%s': %s",
            experiment.name,
            "; ".join(problems),
        )
        raise ElcmPreflightError(problems)
