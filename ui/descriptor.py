"""Construcción y serialización del Dataset Descriptor en YAML.

Es la pieza que convierte a la UI en algo más que un Postman con formularios: lo
que sale del formulario no es una petición, es el **fichero** que el anteproyecto
promete. El usuario puede descargarlo, versionarlo en git y reenviarlo después
sin la UI delante, desde consola o CI. Ver `docs/UI_YAML_MIGRATION.md` §3.

Vive aparte de `streamlit_app.py` a propósito: aquí no se importa Streamlit, así
que estas funciones son puras y se pueden probar sin levantar la UI.

El volcado usa la misma configuración que el servidor al persistir el descriptor
(`app/storage/artifacts.py:_dump_yaml`), para que el fichero que descarga el
usuario y el que queda en `artifacts/` sean el mismo texto. No se reutiliza
`app/rendering/yaml_style.py`: ese entrecomilla todos los valores y convierte los
nulos en cadenas vacías porque lo exige el parser de ELCM, y produce un fichero
incómodo de leer para una persona.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Directorio de descriptores comentados que se ofrecen como plantilla inicial.
# Se resuelve desde el fichero y no desde el cwd porque `streamlit run` se lanza
# indistintamente desde la raíz del repo o desde `ui/`.
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples" / "descriptors"

# Cuerpo de POST /executions/{id}/elcm: no es un Dataset Descriptor completo, solo
# lleva `experiment` y `dataset` (la infraestructura ya existe).
ELCM_EXAMPLE = "05_experimento_elcm.yaml"

# Catalogo de campos editables de cada componente, generado desde
# templates/TNLCM/overlays/. Vive en el mismo directorio que los ejemplos pero no
# es un descriptor, asi que se excluye del desplegable.
COMPONENT_REFERENCE = EXAMPLES_DIR / "REFERENCIA_componentes.yaml"


def to_yaml(data: dict[str, Any]) -> str:
    """Serializa el descriptor con el mismo formato que usa el servidor.

    `sort_keys=False` mantiene el orden en que se construyó el diccionario, que
    es el orden en que se lee el descriptor (infrastructure → experiment →
    dataset → banderas) y no el alfabético.
    """
    return yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        indent=2,
    )


def parse_yaml(text: str) -> dict[str, Any]:
    """Parsea el texto del editor para poder previsualizarlo o validarlo.

    Solo se usa para dar realimentación en el cliente: la validación buena la
    hace el servidor, que además responde con línea y columna. Levanta
    `yaml.YAMLError` si el texto no es YAML.
    """
    data = yaml.safe_load(text)
    if data is None:
        raise ValueError("El descriptor está vacío.")
    if not isinstance(data, dict):
        raise ValueError(f"El descriptor debe ser un mapping YAML, no {type(data).__name__}.")
    return data


def list_examples() -> list[str]:
    """Descriptores de ejemplo disponibles, o lista vacía si no están al lado.

    La UI puede ejecutarse desplegada aparte del repositorio; en ese caso el
    desplegable de ejemplos simplemente no aparece, en vez de romper el arranque.
    """
    if not EXAMPLES_DIR.is_dir():
        return []
    return sorted(path.name for path in EXAMPLES_DIR.glob("*.yaml") if path != COMPONENT_REFERENCE)


def read_example(name: str) -> str:
    """Contenido de un descriptor de ejemplo, con sus comentarios intactos."""
    return (EXAMPLES_DIR / name).read_text(encoding="utf-8")


@dataclass(frozen=True)
class ComponentField:
    """Un campo editable de un componente, identificado por (sección, nombre).

    `ambiguous` marca los nombres que el formato plano no resuelve a una sección
    única. Importa más de lo que parece: el backend **no los rechaza**, los
    asigna en silencio a una de las secciones (la última que gana al construir
    su mapa inverso), de modo que emitir `int_p4_sw.name` en plano escribe en
    `vm` y deja `network.name` inalcanzable, sin ningún aviso.
    """

    component: str
    section: str
    name: str
    required: bool
    ambiguous: bool

    @property
    def label(self) -> str:
        """Cómo se nombra el campo en la interfaz y en el selector."""
        return f"{self.section}.{self.name}" if self.ambiguous else self.name


@lru_cache(maxsize=1)
def _catalog() -> dict[str, dict[str, list[str]]]:
    """`{componente: {sección: [campos]}}`, en el orden del fichero.

    Cacheado porque sin `st.form` la UI se repinta en cada interacción y el
    catálogo son 15 KB de YAML que no cambian mientras corre el proceso.
    """
    if not COMPONENT_REFERENCE.is_file():
        return {}
    try:
        catalog = yaml.safe_load(COMPONENT_REFERENCE.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    if not isinstance(catalog, dict):
        return {}
    return {
        component: {section: list(fields) for section, fields in sections.items()}
        for component, sections in catalog.items()
        if isinstance(sections, dict)
    }


# `campo: ""   # [OBLIGATORIO]`, con la clave a indentación 4 (componente 0, sección 2).
_FIELD_LINE = re.compile(r"^(\s*)([A-Za-z0-9_]+)\s*:")


@lru_cache(maxsize=1)
def _required_paths() -> dict[str, set[tuple[str, str]]]:
    """Los `(sección, campo)` marcados `# [OBLIGATORIO]` en el catálogo.

    `safe_load` no los ve: la marca viaja como comentario al final de la línea,
    así que hay que barrer el texto crudo llevando la cuenta de en qué
    componente y sección vamos. El barrido no puede desincronizarse en silencio:
    `tests/integration/test_component_reference.py` obliga a que todo campo
    obligatorio lleve la marca.
    """
    if not COMPONENT_REFERENCE.is_file():
        return {}

    required: dict[str, set[tuple[str, str]]] = {}
    component: str | None = None
    section: str | None = None

    for line in COMPONENT_REFERENCE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _FIELD_LINE.match(line)
        if not match:
            continue
        indent, key = len(match.group(1)), match.group(2)
        if indent == 0:
            component, section = key, None
        elif indent == 2:
            section = key
        elif indent == 4 and "OBLIGATORIO" in line and component and section:
            required.setdefault(component, set()).add((section, key))

    return required


def list_components() -> list[str]:
    """Nombres de componente que admite `infrastructure.component`.

    Se leen del catalogo publicado en `examples/`, que una prueba mantiene
    sincronizado con los overlays. `GET /health/components` no sirve para esto:
    comprueba la salud de InfluxDB/Grafana/Prometheus/ELCM, no lista los
    componentes de red desplegables.
    """
    return sorted(_catalog())


def component_fields(component: str) -> list[ComponentField]:
    """Campos editables de un componente, en el orden del catálogo.

    Un campo es ambiguo si aparece en más de una sección, o si su nombre es
    también el de una sección del componente: lo primero lo pierde el formato
    plano en silencio, lo segundo lo rechaza el backend con un 400.
    """
    sections = _catalog().get(component, {})
    required = _required_paths().get(component, set())

    seen: dict[str, int] = {}
    for fields in sections.values():
        for name in fields:
            seen[name] = seen.get(name, 0) + 1

    return [
        ComponentField(
            component=component,
            section=section,
            name=name,
            required=(section, name) in required,
            ambiguous=seen[name] > 1 or name in sections,
        )
        for section, fields in sections.items()
        for name in fields
    ]


def _clean_lines(text: str) -> list[str]:
    """Convierte un textarea (una entrada por línea) en lista sin vacíos."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def build_experiment(name: str, testcases: str, ues: str) -> dict[str, Any]:
    """Bloque `experiment` a partir de los tres campos del formulario."""
    experiment: dict[str, Any] = {"name": name.strip()}
    experiment["testcase_paths"] = _clean_lines(testcases)
    ues_paths = _clean_lines(ues)
    if ues_paths:
        experiment["ues_paths"] = ues_paths
    return experiment


def build_dataset(outputs: list[str], variables: dict[str, Any]) -> dict[str, Any]:
    """Bloque `dataset`: los formatos de entrega y sus variables globales.

    Las variables vacías se omiten en vez de emitirse como `null`: el servidor
    rechaza las cadenas vacías (400) y un `null` explícito no dice nada que no
    diga la ausencia de la clave.
    """
    dataset: dict[str, Any] = {"output": list(outputs)}
    for key, value in variables.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        dataset[key] = value.strip() if isinstance(value, str) else value
    return dataset


def _has_value(value: Any) -> bool:
    """True si el valor llega relleno; los vacíos se omiten del descriptor."""
    return value is not None and str(value).strip() != ""


def missing_required(
    selected: Sequence[str],
    values: Mapping[ComponentField, Any],
) -> dict[str, list[str]]:
    """Campos obligatorios sin valor, por componente elegido.

    Generaliza la regla que estaba cableada para `base`. Nombrar un componente
    y no darle sus obligatorios **no** significa «usa los defaults»: el servidor
    responde 400 (`required field missing`). Solo `base` y `mongodb` los tienen;
    el resto sí se despliega con lo que traiga su overlay.
    """
    provided = {field for field, value in values.items() if _has_value(value)}

    missing: dict[str, list[str]] = {}
    for component in selected:
        pending = sorted(
            field.name
            for field in component_fields(component)
            if field.required and field not in provided
        )
        if pending:
            missing[component] = pending
    return missing


def build_component(
    selected: Sequence[str],
    values: Mapping[ComponentField, Any],
) -> dict[str, Any]:
    """Bloque `infrastructure.component` a partir de lo elegido en el formulario.

    Los campos inequívocos viajan planos, que es lo que usan los ejemplos y el
    README y lo que mejor se lee; los ambiguos, anidados bajo su sección, que es
    la única forma de que el valor llegue a la sección que el usuario eligió.
    Mezclar ambas formas en el mismo componente es legal: el contrato del
    servidor despacha por tipo de valor, no por posición.

    Un componente sin ningún valor queda como `componente:` (None), que es como
    se dice «despliégalo con los defaults de su overlay».
    """
    component: dict[str, Any] = {name: None for name in selected}

    for field, value in values.items():
        if field.component not in component or not _has_value(value):
            continue
        block = component[field.component] or {}
        clean = value.strip() if isinstance(value, str) else value
        if field.ambiguous:
            block.setdefault(field.section, {})[field.name] = clean
        else:
            block[field.name] = clean
        component[field.component] = block

    return component


def build_descriptor(
    *,
    name: str,
    component: dict[str, Any],
    parameters: dict[str, str],
    experiment: dict[str, Any] | None,
    dataset: dict[str, Any],
    auto_start_elcm: bool,
    ephemeral_tn: bool,
) -> dict[str, Any]:
    """Ensambla el Dataset Descriptor completo en el orden en que se lee."""
    infrastructure: dict[str, Any] = {"name": name.strip()}
    if component:
        infrastructure["component"] = component
    if parameters:
        infrastructure["parameters"] = parameters

    descriptor: dict[str, Any] = {"infrastructure": infrastructure}
    if experiment is not None:
        descriptor["experiment"] = experiment
    descriptor["dataset"] = dataset
    descriptor["auto_start_elcm"] = auto_start_elcm
    descriptor["ephemeral_tn"] = ephemeral_tn
    return descriptor


def build_elcm_request(*, experiment: dict[str, Any], dataset: dict[str, Any]) -> dict[str, Any]:
    """Cuerpo de POST /executions/{id}/elcm: solo `experiment` y `dataset`."""
    return {"experiment": experiment, "dataset": dataset}
