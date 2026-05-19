from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app import artifacts as artifacts_module
from app.models import ExperimentConfig, InfrastructureConfig
from app.utils.telemetry import telemetry
from app.utils.ytt_renderer import build_tnlcm_values, render_with_ytt, resolve_template_path

logger = logging.getLogger(__name__)


def _ensure_generated_dir(execution_id: str) -> Path:
    generated_dir = Path(artifacts_module._artifact_generated_dir(execution_id))
    generated_dir.mkdir(parents=True, exist_ok=True)
    return generated_dir


def _save_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _timer(service: str, operation: str, execution_id: str):
    timer = telemetry.start_timer(service, operation, execution_id=execution_id)
    timer.start()
    return timer


def _template_or_default(template_ref: str | None, *, category: str, default_ref: str) -> Path:
    ref = template_ref or default_ref
    template_path = resolve_template_path(ref, category=category)
    if template_path is None:
        raise FileNotFoundError(f"Template not found for {category}: {ref}")
    return template_path


def _safe_testcase_name(testcase_ref: str, fallback_index: int) -> str:
    candidate = Path(testcase_ref)
    stem = candidate.stem or f"testcase_{fallback_index + 1:03d}"
    return stem


async def generate_tnlcm_descriptor(
    infra: InfrastructureConfig,
    execution_id: str,
) -> str:
    timer = _timer("generator", "tnlcm_descriptor", execution_id)
    generated_dir = _ensure_generated_dir(execution_id)
    output_path = generated_dir / "tnlcm_descriptor.yaml"

    template_ref = (
        infra.tnlcm_template_ref()
        or infra.parameters.get("descriptor")
        or infra.descriptor_path
        or "TNLCM/tnlcm_descriptor_base.yaml"
    )
    template_path = _template_or_default(
        template_ref,
        category="TNLCM",
        default_ref="TNLCM/tnlcm_descriptor_base.yaml",
    )

    try:
        merged_values = build_tnlcm_values(str(template_path), infra.tnlcm_data_values(), category="TNLCM")
        rendered = render_with_ytt(merged_values, str(template_path), category="TNLCM")
        _save_text(output_path, rendered)
        artifacts_module.persist_generated_artifacts(
            execution_id, tnlcm_descriptor_path=str(output_path)
        )
        logger.info("[%s] TNLCM descriptor generated: %s", execution_id, output_path)
        timer.stop(status="success")
        return str(output_path)
    except Exception:
        timer.stop(status="error")
        raise


async def generate_testcase(
    testcase_ref: str,
    execution_id: str,
    output_index: int = 0,
) -> str:
    timer = _timer("generator", "testcase", execution_id)
    generated_dir = _ensure_generated_dir(execution_id)
    output_path = generated_dir / f"testcase_{output_index + 1:03d}.yml"

    template_path = resolve_template_path(testcase_ref, category="ELCM/TestCase")
    if template_path is None:
        template_path = resolve_template_path(testcase_ref, category="ELCM")
    if template_path is None:
        template_path = resolve_template_path(testcase_ref, category=None)
    if template_path is None:
        raise FileNotFoundError(f"TestCase template not found: {testcase_ref}")

    try:
        rendered = render_with_ytt(
            {
                "execution_id": execution_id,
                "testcase_ref": testcase_ref,
                "testcase_name": _safe_testcase_name(testcase_ref, output_index),
            },
            str(template_path),
            category="ELCM/TestCase",
        )
        _save_text(output_path, rendered)
        artifacts_module.persist_generated_artifacts(
            execution_id, testcase_paths=[str(output_path)]
        )
        logger.info("[%s] TestCase generated: %s", execution_id, output_path)
        timer.stop(status="success")
        return str(output_path)
    except Exception:
        timer.stop(status="error")
        raise


async def generate_experiment_descriptor(
    experiment: ExperimentConfig,
    testcase_refs: list[str],
    execution_id: str,
) -> str:
    timer = _timer("generator", "experiment_descriptor", execution_id)
    generated_dir = _ensure_generated_dir(execution_id)
    output_path = generated_dir / "experiment_descriptor.json"

    template_path = _template_or_default(
        "ELCM/template_experiment_descriptor.json",
        category="ELCM",
        default_ref="ELCM/template_experiment_descriptor.json",
    )

    testcase_names = [_safe_testcase_name(testcase_ref, index) for index, testcase_ref in enumerate(testcase_refs)]

    try:
        rendered = render_with_ytt(
            {
                "Application": experiment.name,
                "TestCases": testcase_names,
                "UEs": experiment.ues_paths,
            },
            str(template_path),
            category="ELCM",
        )

        # Garantizar JSON válido y escritura con formato estable.
        parsed: Any = json.loads(rendered)
        parsed["Application"] = experiment.name
        parsed["TestCases"] = testcase_names
        parsed["UEs"] = experiment.ues_paths
        _save_text(output_path, json.dumps(parsed, indent=4, ensure_ascii=False))
        artifacts_module.persist_generated_artifacts(
            execution_id, experiment_descriptor_path=str(output_path)
        )
        logger.info("[%s] Experiment descriptor generated: %s", execution_id, output_path)
        timer.stop(status="success")
        return str(output_path)
    except Exception:
        timer.stop(status="error")
        raise


