"""Contrato estatico de la biblioteca `templates/ELCM/TestCase/` (TestCases y UEs).

Estos ficheros se suben a ELCM TAL CUAL, sin re-renderizar, asi que nadie los
valida antes de que fallen dentro del experimento. Este modulo comprueba en CI
las reglas del motor ELCM que no se ven a simple vista y que, incumplidas,
producen fallos silenciosos o tumban la fase Run entera.

Es la biblioteca de la que `testcase_paths`/`ues_paths` resuelven por nombre de
fichero, de modo que todo lo que hay aqui es exactamente lo que un body puede
pedir que se suba.
"""

import re
from pathlib import Path

import pytest
import yaml

from app.rendering.paths import elcm_testcase_dir

# Mapa de bandas de Order. El Order es GLOBAL a todo el experimento: las acciones
# de todos los UEs y TestCases se mezclan en una unica lista ordenada por Order.
# Mantener bandas disjuntas es lo que permite combinar varios TestCases sin que
# se entrelacen de forma arbitraria.
ORDER_BANDS: dict[str, tuple[int, int]] = {
    "UE_Variables.yml": (0, 9),
    "UE_Variables_TEMPLATE.yml": (0, 9),
    "TestCase_prometheus_capture.yml": (0, 99),
    "TestCase_prometheus_capture2.yml": (0, 99),
    "TC_ping.yml": (0, 99),
    "TC_V2_BASE_TEMPLATE.yml": (0, 99),
    "TC_Demo_Variables.yml": (100, 119),
    "TC_Demo_Flow.yml": (120, 139),
    "TC_Demo_Python.yml": (140, 159),
    "TC_Util_Inventory.yml": (300, 319),
    "TC_Util_Connectivity.yml": (320, 339),
    "TC_Util_RestApi.yml": (340, 359),
    "TC_Check_PublishTasks.yml": (500, 519),
    "TC_Util_ExportCsv.yml": (820, 839),
}


def _library_files() -> list[Path]:
    # Se resuelve desde la raiz del repo, no desde cwd: la suite vale igual se
    # lance desde donde se lance.
    return sorted(elcm_testcase_dir().glob("*.yml"))


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _is_ue(data: dict) -> bool:
    return "Name" not in data and "Version" not in data


def _first_level_actions(data: dict) -> list[dict]:
    if _is_ue(data):
        return list(next(iter(data.values())))
    return list(data.get("Sequence") or [])


def test_library_is_not_empty():
    # Si la biblioteca se mueve de sitio, el resto de tests se quedaria sin
    # parametrizar y pasaria en verde sin comprobar nada.
    assert _library_files(), f"No hay TestCases en {elcm_testcase_dir()}"


@pytest.mark.parametrize("path", _library_files(), ids=lambda p: p.name)
def test_example_is_parseable_yaml(path: Path):
    assert isinstance(_load(path), dict), f"{path.name} must be a YAML mapping"


@pytest.mark.parametrize("path", _library_files(), ids=lambda p: p.name)
def test_testcase_declares_name_and_version_2(path: Path):
    # ELCM registra los TestCases V2 por su 'Name', y el endpoint de subida
    # rechaza 'Name' sin 'Version: 2' (y viceversa).
    data = _load(path)
    if _is_ue(data):
        pytest.skip("es un fichero UE, no un TestCase")
    assert data.get("Version") == 2, f"{path.name}: falta 'Version: 2'"
    assert data.get("Name"), f"{path.name}: falta 'Name'"


@pytest.mark.parametrize("path", _library_files(), ids=lambda p: p.name)
def test_ue_has_no_testcase_keys(path: Path):
    data = _load(path)
    if not _is_ue(data):
        pytest.skip("es un TestCase, no un UE")
    assert len(data) == 1, f"{path.name}: un UE debe tener exactamente una clave raiz"
    assert isinstance(next(iter(data.values())), list)


@pytest.mark.parametrize("path", _library_files(), ids=lambda p: p.name)
def test_first_level_actions_declare_order_and_task(path: Path):
    # 'Order' es obligatorio en primer nivel y esta PROHIBIDO en los hijos, que
    # usan el orden de la lista (ActionInformation.FromMapping).
    for index, action in enumerate(_first_level_actions(_load(path))):
        assert "Order" in action, f"{path.name}: accion #{index} sin 'Order'"
        assert action.get("Task"), f"{path.name}: accion #{index} sin 'Task'"
        for child in action.get("Children") or []:
            assert "Order" not in child, f"{path.name}: un hijo no puede declarar 'Order'"


@pytest.mark.parametrize("path", _library_files(), ids=lambda p: p.name)
def test_orders_stay_inside_the_declared_band(path: Path):
    band = ORDER_BANDS.get(path.name)
    assert band is not None, (
        f"{path.name} no tiene banda de Order asignada. Anadirla a ORDER_BANDS y "
        f"documentarla en el README para no colisionar con otros TestCases."
    )
    low, high = band
    for action in _first_level_actions(_load(path)):
        order = action["Order"]
        assert low <= order <= high, f"{path.name}: Order {order} fuera de la banda {band}"


@pytest.mark.parametrize("path", _library_files(), ids=lambda p: p.name)
def test_no_expansion_default_contains_a_colon(path: Path):
    # El Expander hace capture.split(':') SIN limite, asi que "@[Url:http://x]"
    # lanza ValueError; y como la expansion vive fuera del try/except del
    # executor, se lleva por delante la fase Run entera.
    text = path.read_text(encoding="utf-8")
    offenders = [cap for cap in re.findall(r"@\[(.*?)]", text) if cap.count(":") > 1]
    assert not offenders, f"{path.name}: defaults con ':' que rompen el Expander: {offenders}"


@pytest.mark.parametrize("path", _library_files(), ids=lambda p: p.name)
def test_publish_from_file_keys_are_two_element_pairs(path: Path):
    # 'Keys' se recorre como `for index, key in keys`: cada entrada tiene que ser
    # un par [indice_de_grupo, nombre]. YAML no tiene tuplas.
    def check(node):
        if isinstance(node, dict):
            if str(node.get("Task", "")).startswith("Run.PublishFrom"):
                for entry in node.get("Config", {}).get("Keys") or []:
                    assert (
                        isinstance(entry, list) and len(entry) == 2
                    ), f"{path.name}: 'Keys' debe ser una lista de pares [grupo, nombre]"
                    assert isinstance(entry[0], int)
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for item in node:
                check(item)

    check(_load(path))
