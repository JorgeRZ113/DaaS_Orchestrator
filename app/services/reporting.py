"""Persistencia best-effort de la telemetria de una ejecucion.

Se invoca en cada hito de las fases. Es best-effort a proposito: un fallo de
disco escribiendo un informe no puede abortar un despliegue que va bien.
"""

import logging

from app.core.config import settings
from app.services import state
from app.storage import artifacts

logger = logging.getLogger(__name__)


async def persist_telemetry_report_best_effort(execution_id: str, stage: str) -> str | None:
    """Persist telemetry report and execution summary without interrupting orchestration.

    Escribe los dos canales en cada hito: el informe tecnico
    (`telemetry_report_<stage>.json`) y el resumen legible para el
    experimentador (`summary.json` + `summary.md`). Ambas escrituras son
    best-effort; un fallo de I/O no aborta la orquestacion.
    """
    if not settings.telemetry_report_artifacts:
        return None

    try:
        telemetry_path = await artifacts.build_telemetry_report_artifact(execution_id, stage)
    except Exception as exc:
        logger.warning(
            "[%s] Could not persist telemetry report for stage %s: %s",
            execution_id,
            stage,
            exc,
        )
        return None

    record = state.executions.get(execution_id)
    if record:
        generated = [telemetry_path]
        try:
            generated.extend(
                await artifacts.build_execution_summary_artifacts(execution_id, record)
            )
        except Exception as exc:
            logger.warning(
                "[%s] Could not persist execution summary for stage %s: %s",
                execution_id,
                stage,
                exc,
            )
        merged_artifacts = list(dict.fromkeys([*record.artifacts, *generated]))
        state.update(execution_id, artifacts=merged_artifacts)
    return telemetry_path
