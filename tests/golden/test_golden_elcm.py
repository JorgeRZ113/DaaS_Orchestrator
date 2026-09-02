"""G3: descriptor de referencia -> artefactos ELCM, congelados byte a byte.

Tres ficheros: el Experiment Descriptor y los dos TestCases de dataset (csv y
dashboard) que el orquestador genera e inyecta en el experimento.

A diferencia de un test de render aislado, aqui se recorre la CADENA REAL:
el bloque `dataset` del descriptor y el TestCase de captura del experimento se
resuelven con `_dataset_data_values`, que es la funcion que usa la fase ELCM en
produccion, y su salida alimenta a ytt. Asi el golden cubre tambien la
precedencia de variables (body -> derivado del despliegue -> default del
overlay), que es donde vive la logica y no solo la plantilla.

Sigue sin tocar la red: el unico dato del despliegue que hace falta -la IP de
monitorizacion- se aporta como un report TNLCM de fichero.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from app.adapters import elcm
from app.api.body_formats import JsonLikeLoader
from app.domain.descriptor import DatasetDescriptor
from app.rendering.elcm.dataset import generate_elcm_dataset_testcase
from app.services.phases.elcm import _dataset_data_values
from app.storage.artifacts import artifact_root_dir

pytestmark = pytest.mark.usefixtures("isolate_artifacts_dir")

requires_ytt = pytest.mark.skipif(shutil.which("ytt") is None, reason="ytt binary not available")

DESCRIPTORS_DIR = Path(__file__).resolve().parents[2] / "examples" / "descriptors"

EXECUTION_ID = "golden-elcm-dataset"

# IP de monitorizacion de la TN, DISTINTA A PROPOSITO de la que trae el overlay
# por defecto (192.168.199.2, la de laboratorio). Si coincidieran, el golden no
# distinguiria "derivado del report" de "default del overlay" y el caso no
# probaria nada: con una IP distinta, que la derivacion se rompa cambia el
# fichero y el test falla.
# Sin credenciales: el token del report es secreto y no se persiste (regla 8.7).
MONITORING_REPORT = {
    "summary": {
        "monitoring": {
            "ip": "10.11.12.13",
            "ports": [8086, 3000, 9090],
        }
    }
}


def _load_descriptor(name: str) -> DatasetDescriptor:
    raw = yaml.load((DESCRIPTORS_DIR / name).read_text(encoding="utf-8"), Loader=JsonLikeLoader)
    return DatasetDescriptor.model_validate(raw)


@pytest.fixture
def monitoring_report() -> None:
    """Deja un report TNLCM en disco para que la IP de InfluxDB se derive de el."""
    base = Path(artifact_root_dir()) / EXECUTION_ID
    base.mkdir(parents=True, exist_ok=True)
    (base / "tnlcm_report_summary.json").write_text(json.dumps(MONITORING_REPORT), encoding="utf-8")


@pytest.mark.asyncio
async def test_experiment_descriptor_renders_unchanged(golden) -> None:
    """El Experiment Descriptor que se envia a ELCM.

    Lo que protege, y no es cosmetico: los TestCases y los UEs se referencian por
    su `Name:` INTERNO, no por el nombre del fichero. ELCM registra por `Name:`,
    asi que referenciar por fichero produce un descriptor que ELCM acepta y luego
    no resuelve -- un fallo que ya costo una ejecucion completa (`tn_deveop_21_4`).
    """
    descriptor = _load_descriptor("04_dataset_completo.yaml")
    experiment = descriptor.experiment
    testcase_paths = [
        str(elcm.resolve_testcase_file(ref)) for ref in experiment.testcase_paths if ref
    ]

    output_path = Path(
        await elcm.generate_experiment_descriptor(
            experiment, testcase_paths, execution_id=EXECUTION_ID
        )
    )

    golden(
        output_path.read_text(encoding="utf-8"),
        "elcm_dataset_completo/experiment_descriptor_exp-dataset-completo.json",
    )


@requires_ytt
@pytest.mark.asyncio
async def test_csv_dataset_testcase_renders_unchanged(golden, monitoring_report) -> None:
    """El TestCase que entrega el dataset en CSV.

    Protege el entrecomillado que exige ELCM sobre la `CustomQuery` de Flux, que
    lleva comillas embebidas (`bucket: "testing"`, `r["ExecutionId"]`) y tiene que
    salir escapada sin romper el YAML, en una sola linea (`width=4096`, o PyYAML
    la parte y la query deja de ser valida).

    Ojo al leer el esperado: `Run.CompressFiles` (Order 801) va SIN la puerta
    `Flow.Select`/`ZipDelivery` que si llevan los TestCases del catalogo, y no es
    un descuido -- es la unica tarea que registra el CSV en `GeneratedFiles`, que
    es lo que ELCM copia a `Results/<id>.zip`. Sin ella el fichero no llega a
    `/results`. Como esa tarea esta rota en todas las versiones publicadas del
    motor (ver docs/INCIDENCIA_ELCM_VERSION_DESPLEGADA.md), este golden congela
    el comportamiento de HOY; cuando el motor se parchee habra que regenerarlo.
    """
    descriptor = _load_descriptor("04_dataset_completo.yaml")
    testcase_paths = [
        str(elcm.resolve_testcase_file(ref)) for ref in descriptor.experiment.testcase_paths if ref
    ]
    data_values = _dataset_data_values(
        "csv", EXECUTION_ID, descriptor.dataset.variables(), testcase_paths
    )

    output_path = await generate_elcm_dataset_testcase(
        "csv", execution_id=EXECUTION_ID, data_values=data_values
    )

    golden(
        output_path.read_text(encoding="utf-8"),
        "elcm_dataset_completo/TC_Dataset_PrometheusToCsv.yml",
    )


@requires_ytt
@pytest.mark.asyncio
async def test_dashboard_dataset_testcase_renders_unchanged(golden, monitoring_report) -> None:
    """El TestCase que crea el dashboard de Grafana.

    ELCM solo sabe construir paneles declarados uno por metrica, asi que el
    numero y el orden de los paneles salen de las `QueriesRange` del TestCase de
    captura del experimento, y la rejilla la calcula una funcion de ytt. Congelar
    la salida ata las tres cosas a la vez: que la deteccion de la captura sigue
    encontrandola, que el filtro de metricas agregadas sigue descartandolas, y
    que el layout no baila.
    """
    descriptor = _load_descriptor("04_dataset_completo.yaml")
    testcase_paths = [
        str(elcm.resolve_testcase_file(ref)) for ref in descriptor.experiment.testcase_paths if ref
    ]
    data_values = _dataset_data_values(
        "dashboard", EXECUTION_ID, descriptor.dataset.variables(), testcase_paths
    )

    output_path = await generate_elcm_dataset_testcase(
        "dashboard", execution_id=EXECUTION_ID, data_values=data_values
    )

    golden(
        output_path.read_text(encoding="utf-8"),
        "elcm_dataset_completo/TC_Dataset_PrometheusToGrafana.yml",
    )
