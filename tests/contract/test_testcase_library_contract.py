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
#
# Cada TestCase es autocontenido -captura Y entrega en el mismo fichero- asi que
# ocupa un BLOQUE DE 100 propio, y el numero del nombre coincide con su bloque.
# El orden de los bloques ya deja el preflight antes de las capturas.
ORDER_BANDS: dict[str, tuple[int, int]] = {
    # Los ficheros UE publican variables globales y van todos en Order 0.
    "UE_1_Preflight.yml": (0, 9),
    "UE_2_Prometheus_Capture_Generico.yml": (0, 9),
    "UE_3_Prometheus_Capture_Open5GS.yml": (0, 9),
    "UE_4_Dataset_Csv.yml": (0, 9),
    "UE_5_Flujo_Variables.yml": (0, 9),
    "UE_6_Latencia_SLA.yml": (0, 9),
    "UE_Variables_TEMPLATE.yml": (0, 9),
    "TC_1_Preflight.yml": (100, 199),
    "TC_2_Prometheus_Capture_Generico.yml": (200, 299),
    "TC_3_Prometheus_Capture_Open5GS.yml": (300, 399),
    "TC_4_Dataset_Csv.yml": (400, 499),
    "TC_5_Flujo_Variables.yml": (500, 599),
    "TC_6_Latencia_SLA.yml": (600, 699),
    # Esqueleto para escribir TestCases nuevos, no ejecutable en composicion.
    "TC_V2_BASE_TEMPLATE.yml": (700, 799),
}


# Claves que el motor deja siempre en el diccionario de valores publicados: se
# pueden leer con @[...] aunque ningun UE las publique. Salen del propio log de
# ELCM ("Available keys: [...]").
ENGINE_PUBLISHED_KEYS: frozenset[str] = frozenset(
    {"Descriptor", "UserId", "ExecutionId", "Configuration", "DeployedSliceId", "PreviousTaskLog"}
)

# Referencias a variables inexistentes puestas A PROPOSITO para ensenar que hace
# el motor cuando la clave no existe. Cualquier otra ausencia es un error.
DELIBERATELY_UNDEFINED: dict[str, set[str]] = {
    "TC_5_Flujo_Variables.yml": {"VariableQueNoExiste"},
}

# Tasks cuyo parametro 'Key' LEE un valor publicado. En Run.Evaluate, en cambio,
# 'Key' es el nombre bajo el que PUBLICA el resultado.
_TASKS_THAT_READ_KEY = {"Run.UpgradeVerdict", "Flow.While"}

_EXPANSION = re.compile(r"@\[([^\]]+)\]")


def _walk_strings(node) -> list[str]:
    # Recorre solo los valores del YAML ya parseado, de modo que los comentarios
    # (que documentan variables de otros TestCases) no cuentan como uso real.
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for value in node.values() for s in _walk_strings(value)]
    if isinstance(node, list):
        return [s for item in node for s in _walk_strings(item)]
    return []


def _walk_actions(node):
    if isinstance(node, dict):
        if node.get("Task"):
            yield node
        for value in node.values():
            yield from _walk_actions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_actions(item)


def _consumed_names(data: dict) -> set[str]:
    names = set()
    for text in _walk_strings(data):
        for capture in _EXPANSION.findall(text):
            name = capture.split(":", 1)[0]
            # "@[Grupo.Clave]" fija el grupo de forma explicita; el nombre real
            # depende del grupo, asi que no se puede comprobar aqui.
            if "." not in name:
                names.add(name)
    for action in _walk_actions(data):
        config = action.get("Config") or {}
        if action["Task"] in _TASKS_THAT_READ_KEY and config.get("Key"):
            names.add(config["Key"])
        for condition in config.get("Conditions") or []:
            if isinstance(condition, dict) and condition.get("Key"):
                names.add(condition["Key"])
    return names


def _produced_names(data: dict) -> set[str]:
    names = set()
    for action in _walk_actions(data):
        task = action["Task"]
        config = action.get("Config") or {}
        if task == "Run.Publish":
            names.update(config.keys())
        elif task == "Run.Evaluate" and config.get("Key"):
            names.add(config["Key"])
        elif task.startswith("Run.PublishFrom"):
            for entry in config.get("Keys") or []:
                if isinstance(entry, list) and len(entry) == 2:
                    names.add(entry[1])
    return names


def _all_ue_published_keys() -> set[str]:
    keys = set()
    for path in _library_files():
        data = _load(path)
        if _is_ue(data):
            keys.update(_produced_names(data))
    return keys


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
def test_testcase_inputs_are_published_by_some_ue(path: Path):
    # Un TestCase consume dos clases de variables: las que produce el mismo
    # durante la ejecucion (salidas) y las que espera encontrar ya publicadas
    # (entradas). Las entradas tienen que estar en algun UE de la biblioteca: si
    # no, se expanden a <<UNDEFINED>>, que es una cadena valida y no falla, sino
    # que se cuela hasta la task como si fuera un valor bueno.
    data = _load(path)
    if _is_ue(data):
        pytest.skip("es un fichero UE, no un TestCase")

    inputs = _consumed_names(data) - _produced_names(data)
    known = (
        _all_ue_published_keys()
        | ENGINE_PUBLISHED_KEYS
        | DELIBERATELY_UNDEFINED.get(path.name, set())
    )

    missing = sorted(inputs - known)
    assert not missing, (
        f"{path.name}: consume {missing} y no lo publica ningun UE de la biblioteca. "
        f"Anadirlo a su UE_*.yml y a UE_Variables_TEMPLATE.yml, o corregir el nombre."
    )


@pytest.mark.parametrize("path", _library_files(), ids=lambda p: p.name)
def test_library_does_not_use_experiment_parameters(path: Path):
    # ELCM tiene DOS mecanismos de variables: el bloque 'Parameters' del TestCase
    # (que se lee con @[Params.X] y se vuelca entero en @{JSONParameters}) y los
    # valores publicados por los ficheros UE. Esta biblioteca usa solo el segundo,
    # para que todas las variables de un experimento esten en un unico sitio.
    #
    # Ojo: 'Parameters' DENTRO de un Config es otra cosa -la linea de comandos de
    # Run.CliExecute- y si esta permitido. Por eso se mira la raiz del documento.
    data = _load(path)
    assert "Parameters" not in data, (
        f"{path.name}: declara el bloque 'Parameters'. Las variables van en su "
        f"fichero UE_*.yml, no en 'Parameters'."
    )

    offenders = [
        text for text in _walk_strings(data) if "@[Params." in text or "@{JSONParameters}" in text
    ]
    assert not offenders, (
        f"{path.name}: usa la expansion del bloque 'Parameters' ({offenders}). "
        f"Publicar el valor en el UE y leerlo con @[Nombre]."
    )


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
