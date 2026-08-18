import shutil
from pathlib import Path

import pytest
import yaml

from app.rendering.elcm.dataset import (
    ELCM_DATASET_TEMPLATES,
    generate_elcm_dataset_testcase,
)

pytestmark = pytest.mark.usefixtures("isolate_artifacts_dir")

# El render real invoca el binario `ytt`; si no está disponible, se omiten los
# tests que lo necesitan (en CI ytt es un requisito).
ytt_available = shutil.which("ytt") is not None
requires_ytt = pytest.mark.skipif(not ytt_available, reason="ytt binary not available")


@pytest.mark.asyncio
@requires_ytt
async def test_generate_csv_dataset_testcase_renders_valid_yaml():
    output_path = await generate_elcm_dataset_testcase("csv", execution_id="exec-ds-csv")

    output_file = Path(output_path)
    assert output_file.exists()
    # El fichero se nombra por el `Name:` interno para que su stem coincida con
    # el nombre por el que ELCM lo referencia al inyectarlo en el experimento.
    assert output_file.name == "TC_Dataset_PrometheusToCsv.yml"
    assert output_file.parent.name == "archivos_generados"

    payload = yaml.safe_load(output_file.read_text(encoding="utf-8"))
    assert payload["Version"] == 2
    assert payload["Name"] == "TC_Dataset_PrometheusToCsv"
    # El CSV se entrega comprimiendo el fichero generado (Run.CompressFiles).
    assert isinstance(payload["Sequence"], list) and payload["Sequence"]


@pytest.mark.asyncio
@requires_ytt
async def test_generate_csv_dataset_testcase_quotes_all_strings():
    # ELCM es muy sensible al entrecomillado: el TestCase generado debe llevar los
    # strings entre comillas dobles, y los que tienen comillas embebidas (la
    # CustomQuery Flux) se escapan sin romper el YAML.
    output_path = await generate_elcm_dataset_testcase("csv", execution_id="exec-ds-quote")
    text = Path(output_path).read_text(encoding="utf-8")

    # Token del Expander, entre comillas.
    assert '"@{ExecutionId}"' in text
    # La CustomQuery Flux lleva comillas embebidas (bucket "testing", tags r["..."])
    # que deben emitirse ESCAPADAS.
    assert r"\"testing\"" in text
    assert r"r[\"ExecutionId\"]" in text
    # Re-parsea correctamente y la query recupera su forma original (sin escapar).
    payload = yaml.safe_load(text)
    custom_query = payload["Sequence"][0]["Config"]["CustomQuery"]
    assert 'from(bucket: "testing")' in custom_query
    assert 'r["ExecutionId"] == "@{ExecutionId}"' in custom_query


@pytest.mark.asyncio
@requires_ytt
async def test_generate_dashboard_generates_one_panel_per_metric():
    # measurement + metrics se inyectan como data values (los lee el orquestador del
    # TestCase de captura). El template genera un panel por metrica con layout auto.
    output_path = await generate_elcm_dataset_testcase(
        "dashboard",
        execution_id="exec-ds-dash",
        data_values={
            "dataset": {
                "measurement": "OPEN5GS_GRAFANA",
                "metrics": ["ues_active", "ran_ue", "gnb"],
            }
        },
    )

    output_file = Path(output_path)
    assert output_file.name == "TC_Dataset_PrometheusToGrafana.yml"

    payload = yaml.safe_load(output_file.read_text(encoding="utf-8"))
    assert payload["Name"] == "TC_Dataset_PrometheusToGrafana"
    dashboard = payload["Dashboard"]
    assert [p["Field"] for p in dashboard] == ["ues_active", "ran_ue", "gnb"]
    assert all(p["Measurement"] == "OPEN5GS_GRAFANA" for p in dashboard)
    # Layout automatico en rejilla de 2 columnas (ancho 12, alto 8).
    assert [p["Position"] for p in dashboard] == [[0, 0], [12, 0], [0, 8]]


@pytest.mark.asyncio
@requires_ytt
async def test_generate_dashboard_without_metrics_is_empty():
    # Sin data values, metrics=[] del overlay base -> Dashboard vacio (valido).
    output_path = await generate_elcm_dataset_testcase("dashboard", execution_id="exec-ds-empty")

    payload = yaml.safe_load(Path(output_path).read_text(encoding="utf-8"))
    assert payload["Dashboard"] == []


@pytest.mark.asyncio
async def test_generate_dataset_testcase_rejects_unknown_kind():
    with pytest.raises(ValueError) as exc_info:
        await generate_elcm_dataset_testcase("zip", execution_id="exec-ds-bad")

    assert "zip" in str(exc_info.value)


def test_elcm_dataset_templates_registry_has_csv_and_dashboard():
    assert set(ELCM_DATASET_TEMPLATES) == {"csv", "dashboard"}


# --- Variables globales del bloque `dataset` inyectadas en el render ---


@pytest.mark.asyncio
@requires_ytt
async def test_csv_dataset_uses_overlay_defaults_when_nothing_is_injected():
    output_path = await generate_elcm_dataset_testcase("csv", execution_id="exec-ds-defaults")
    payload = yaml.safe_load(Path(output_path).read_text(encoding="utf-8"))
    config = payload["Sequence"][0]["Config"]

    assert config["Host"] == "192.168.199.2"
    assert config["Port"] == 8086
    assert '"OPEN5GS_KPIS"' in config["CustomQuery"]
    assert 'from(bucket: "testing")' in config["CustomQuery"]


@pytest.mark.asyncio
@requires_ytt
async def test_csv_dataset_injects_measurement_bucket_and_influx_host():
    # Lo que el orquestador construye a partir de dataset.measurement /
    # dataset.influx_host / dataset.influx_port / dataset.influx_bucket.
    data_values = {
        "dataset": {
            "measurement": "MI_MEASUREMENT",
            "bucket": "mi_bucket",
            "influx": {"host": "10.11.27.5", "port": 8087},
        }
    }
    output_path = await generate_elcm_dataset_testcase(
        "csv", execution_id="exec-ds-vars", data_values=data_values
    )
    payload = yaml.safe_load(Path(output_path).read_text(encoding="utf-8"))
    config = payload["Sequence"][0]["Config"]

    assert config["Host"] == "10.11.27.5"
    assert config["Port"] == 8087
    assert 'from(bucket: "mi_bucket")' in config["CustomQuery"]
    assert '"_measurement"] == "MI_MEASUREMENT"' in config["CustomQuery"]
    # La query Flux tiene que quedar en UNA sola linea: ELCM no la acepta partida.
    assert "\n" not in config["CustomQuery"]


@pytest.mark.asyncio
@requires_ytt
async def test_csv_dataset_orders_are_in_the_delivery_band():
    # Banda 800-899 (entrega): por debajo iria antes que los TestCases
    # funcionales del usuario (100-699) y el CSV saldria vacio.
    output_path = await generate_elcm_dataset_testcase("csv", execution_id="exec-ds-orders")
    payload = yaml.safe_load(Path(output_path).read_text(encoding="utf-8"))

    orders = [action["Order"] for action in payload["Sequence"]]
    assert orders == [800, 801]
