"""G1 y G2: descriptor de referencia -> descriptor TNLCM, congelado byte a byte.

Cubre la actividad F6.3 del anteproyecto ("3-5 descriptores de referencia con
outputs esperados conocidos, para regression testing") y F7.1.4 ("validar que
los descriptores de referencia siguen funcionando tras cambios en el codigo").

Las entradas son los ficheros de `examples/descriptors/`, no descriptores
inventados aqui: son los mismos que documentan el formato y los que el usuario
copia, asi que una regresion en el generador y una regresion en la documentacion
se detectan con la misma prueba. Se cargan por el mismo camino que una peticion
real (`JsonLikeLoader` + el modelo Pydantic).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from app.api.body_formats import JsonLikeLoader
from app.domain.descriptor import DatasetDescriptor
from app.rendering.tnlcm.renderer import generate_tnlcm_descriptor

pytestmark = pytest.mark.usefixtures("isolate_artifacts_dir")

# La etapa `unit` de CI no instala ytt; la etapa `pytest` fija v0.55.1.
requires_ytt = pytest.mark.skipif(shutil.which("ytt") is None, reason="ytt binary not available")

DESCRIPTORS_DIR = Path(__file__).resolve().parents[2] / "examples" / "descriptors"


def _load_descriptor(name: str) -> DatasetDescriptor:
    raw = yaml.load((DESCRIPTORS_DIR / name).read_text(encoding="utf-8"), Loader=JsonLikeLoader)
    return DatasetDescriptor.model_validate(raw)


@requires_ytt
@pytest.mark.asyncio
async def test_minimal_base_descriptor_renders_unchanged(golden) -> None:
    """G1: el caso minimo, un solo componente.

    Congela la cadena `tn_init -> monitoring-test -> elcm-exp` que el generador
    monta siempre, y el entrecomillado estricto que exige ELCM (todo string
    entre comillas dobles, `None` como cadena vacia).
    """
    descriptor = _load_descriptor("01_minimo_base.yaml")

    output_path = Path(
        await generate_tnlcm_descriptor(descriptor.infrastructure, execution_id="golden-01-minimo")
    )

    golden(
        output_path.read_text(encoding="utf-8"),
        "01_minimo_base/tnlcm_descriptor.yaml",
    )
    # El overlay relleno es la Fase 1 del pipeline: si se rompe la cabecera
    # `#@data/values` o el merge de valores, ytt renderiza los defaults y el
    # descriptor final sale plausible pero equivocado. Congelarlo separa las dos
    # fases al diagnosticar.
    golden(
        (output_path.parent / "base_overlay_filled.yaml").read_text(encoding="utf-8"),
        "01_minimo_base/base_overlay_filled.yaml",
    )


@requires_ytt
@pytest.mark.asyncio
async def test_multi_component_descriptor_renders_unchanged(golden) -> None:
    """G2: dos componentes en el descriptor, cinco nodos en la salida.

    `ueransim_both` es una plantilla COMPUESTA: expande a `vnet`, `open5gs_vm` y
    `ueransim` encadenados por dependencias. Es el caso que mas superficie
    protege:

    - Coercion de tipos. `one_open5gs_vm_mcc: "001"` tiene que seguir siendo un
      string entrecomillado; si el round-trip de YAML lo convierte en el entero
      1 se pierde el cero inicial y el core arranca con otro PLMN. Al lado,
      `one_vnet_netmask: 24` y `one_open5gs_vm_ue_count: 20` tienen que seguir
      siendo enteros SIN comillas. Es una regresion silenciosa: el YAML sigue
      siendo valido y el despliegue falla mucho mas tarde.
    - Dependencias cruzadas entre componentes
      (`one_ueransim_gnb_linked_5gcore: "open5gs_vm-core"`).
    - Campos opcionales vacios que se preservan (`one_open5gs_vm_key: ""`).
    - Orden determinista de los componentes: `base` primero y el resto
      alfabetico, para que el fichero no baile entre ejecuciones.
    """
    descriptor = _load_descriptor("04_dataset_completo.yaml")

    output_path = Path(
        await generate_tnlcm_descriptor(descriptor.infrastructure, execution_id="golden-04-dataset")
    )

    golden(
        output_path.read_text(encoding="utf-8"),
        "04_dataset_completo/tnlcm_descriptor.yaml",
    )
