# Golden tests

Descriptores de referencia con la salida congelada byte a byte. Cubren dos
compromisos del anteproyecto:

- **F6.3** — «Crear suite de "golden tests": 3-5 descriptores de referencia con
  outputs esperados conocidos, para regression testing.»
- **F7.1.4** — «Tests de regresión: validar que los 3-5 descriptores de
  referencia siguen funcionando tras cambios en el código.»

No cubren el **Objetivo 5** del anteproyecto. Ese pide evidencia de laboratorio
—comparativa entre el proceso manual y el automatizado, y un experimento de
repetición documentado— que es trabajo de campo, no de código. Lo que estos
tests aportan a ese argumento es una pieza concreta: que la transformación
descriptor → artefactos es reproducible.

## Qué congela cada caso

| Caso | Entrada | Salida congelada |
|---|---|---|
| **G1** | `examples/descriptors/01_minimo_base.yaml` | `tnlcm_descriptor.yaml` + `base_overlay_filled.yaml` |
| **G2** | `examples/descriptors/04_dataset_completo.yaml` | `tnlcm_descriptor.yaml` (6 nodos) |
| **G3** | `examples/descriptors/04_dataset_completo.yaml` | Experiment Descriptor + los 2 TestCases de dataset |
| **G4** | ZIP de resultados reconstruido | Árbol de `result/<experimento>/` tras la limpieza |

Las entradas de G1–G3 son los mismos ficheros de `examples/descriptors/` que
documentan el formato, no descriptores inventados para la prueba: así una
regresión del generador y una regresión de la documentación se detectan con el
mismo test.

## Por qué esto es determinista

El contenido generado no lleva `execution_id`, ni timestamps, ni rutas
absolutas. El `execution_id` solo aparece en la *ruta* de salida y, dentro de los
TestCases de dataset, como el literal `"@{ExecutionId}"`, que es un token del
Expander de ELCM y se resuelve en su runtime, no aquí.

El único vector real de no-determinismo es la versión del binario `ytt`, fijada a
`v0.55.1` en CI y en `CLAUDE.md` §5. Los casos que lo necesitan se saltan si no
está en el `PATH`.

## Qué protege cada uno, en concreto

**G2** es el de más superficie. `ueransim_both` es una plantilla *compuesta*: de
dos componentes declarados salen seis nodos encadenados por dependencias. Ahí
vive la coerción de tipos, que es donde aparecen las regresiones silenciosas:
`one_open5gs_vm_mcc: "001"` tiene que seguir siendo un string entrecomillado —si
el round-trip de YAML lo convierte en el entero `1` se pierde el cero inicial y
el core arranca con otro PLMN— mientras que `one_vnet_netmask: 24` y
`one_open5gs_vm_ue_count: 20` tienen que seguir siendo enteros sin comillas. El
YAML sigue siendo válido en los dos casos; el despliegue falla mucho más tarde.

**G3** ata que los TestCases y los UEs se referencian por su `Name:` interno y no
por el nombre del fichero. ELCM registra por `Name:`, así que referenciar por
fichero produce un descriptor que ELCM acepta y luego no resuelve: un fallo que
ya costó una ejecución completa (`tn_deveop_21_4`).

**G4** es el único con entrada fabricada, y tiene que serlo: `Run.CompressFiles`
está roto en las nueve versiones publicadas de ELCM y su excepción aborta el
experimento entero, así que `GET /execution/<id>/results` llega vacío contra la
TN real (ver `docs/INCIDENCIA_ELCM_VERSION_DESPLEGADA.md`). No es evitable desde
aquí: `Run.InfluxToCsv` escribe el CSV pero no lo registra en `GeneratedFiles`, y
`Run.CompressFiles` es la única tarea que hace ese registro.

La entrada se **reconstruye, no se inventa**. La forma del ZIP y el contenido de
los ficheros están copiados de dos ejecuciones reales de la TN `tn_deveop_22_2`
del 23 de agosto de 2026, leyendo sus logs: la execution 11 (`TC_4_Dataset_Csv`)
aporta el CSV exportado de InfluxDB, y la 13 (`TC_6_Latencia_SLA`) las medidas de
rtt y el inventario del entorno. Son dos bundles distintos a propósito: el
segundo no contiene ningún CSV, y sobre un bundle que solo tuviera CSV las
entregas `csv` y `files` darían el mismo resultado y congelarlas no probaría
nada.

## Regenerar los esperados

```bash
GOLDEN_UPDATE=1 python -m pytest -m golden
```

Reescribe los ficheros de `expected/` y lista al final cuáles ha tocado. **Esa
pasada no comprueba nada**: revisa el diff fichero a fichero antes de commitear.
Un golden aceptado a ciegas congela un bug con la misma fidelidad que el
comportamiento correcto.

El helper se niega a regenerar si detecta `CI` en el entorno: allí un
`GOLDEN_UPDATE` colado convertiría la puerta de regresión en un sello de goma.

## Ejecutar solo este nivel

```bash
python -m pytest -m golden
```

El marcador sale del nombre del directorio (`tests/conftest.py`,
`pytest_collection_modifyitems`), igual que el resto de niveles de la pirámide.
