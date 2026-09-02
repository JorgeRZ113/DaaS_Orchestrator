"""CLI cliente del DaaS Orchestrator (`daas`).

Cubre la actividad F6.3 del anteproyecto: lanzar y gestionar ejecuciones desde
consola, sin navegador y sin Postman. Es lo que permite encadenar el ciclo
completo en un script de shell o en CI:

    daas run descriptor.yaml && daas elcm "$ID" experimento.yaml && daas download "$ID"

Habla con la API por HTTP como un cliente cualquiera —**no** importa el
orquestador ni comparte proceso con el— reutilizando `app/client.py`, el mismo
modulo que usa la UI de Streamlit. Que el cliente sea uno solo es el motivo de
que viva en `app/` y no bajo `ui/`.

A proposito NO se importa `app.core.config`: el CLI puede apuntar a un servidor
remoto, y exigir un `.env` local valido para leer una variable de entorno seria
atarlo a la maquina donde corre el servicio. La configuracion sale de los
argumentos y del entorno, en ese orden.

Codigos de salida, pensados para encadenar con `&&`:

    0  correcto
    1  error de la API o de red
    2  error de uso (argumentos, fichero ilegible, falta la API key)
    3  completado PARCIALMENTE (HTTP 207)

El 3 tiene codigo propio porque un 207 no es un exito: la TN esta desplegada
pero el tunel WireGuard hay que montarlo a mano, asi que el `daas elcm` que
viniera detras fallaria. Con exit 0 la cadena seguiria adelante a ciegas.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.client import (
    DEFAULT_BASE_URL,
    ApiClient,
    ApiError,
    Descriptor,
    PhaseResult,
)

EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3

# Variables de entorno que evitan repetir --base-url y --api-key en cada orden.
# `API_KEY` es el nombre que ya usa el `.env` del servidor, asi que en la maquina
# de desarrollo el CLI funciona sin configurar nada aparte.
ENV_BASE_URL = "DAAS_BASE_URL"
ENV_API_KEY = "DAAS_API_KEY"
ENV_API_KEY_FALLBACK = "API_KEY"


def _force_utf8_output() -> None:
    """Pone stdout y stderr en UTF-8 antes de imprimir nada.

    Lo que se imprime no es ASCII: el resumen en Markdown que devuelve el
    servidor trae emoji (❌, ✅) y los mensajes de error, tildes. Cuando el flujo
    de salida no admite esos caracteres, `print` no los sustituye: LEVANTA
    `UnicodeEncodeError` y la orden muere con un traceback.

    Pasa en los dos sistemas, por caminos distintos, y ninguno es raro:

    - en Windows la consola usa cp1252 (asi se descubrio: `daas summary
      --format md` reventaba contra el servidor de desarrollo);
    - en Linux, con `LANG=C` o `LC_ALL=POSIX`, Python deja stdout en ASCII y
      falla exactamente igual. Es la configuracion por defecto de muchas
      imagenes de contenedor minimas, de `cron` y de las unidades de systemd,
      que es justo donde se espera que corra un CLI sin persona delante.

    Forzar UTF-8 arregla los dos casos y no estropea ninguno: un terminal
    moderno ya venia en UTF-8, y una redireccion a fichero produce un fichero
    UTF-8, que es lo deseable para un informe con emoji.

    Se hace en `main()` y no al importar porque reconfigurar los flujos del
    proceso es un efecto secundario que un modulo importable no debe provocar.
    `reconfigure` solo existe en `TextIOWrapper`, asi que se comprueba: bajo la
    captura de pytest stdout es otra cosa y no hace falta tocarlo.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _print_json(payload: Any) -> None:
    """Vuelca el payload en stdout como JSON indentado.

    `ensure_ascii=False` para que los mensajes de error del servidor se lean con
    sus tildes, y no como `\\u00f3`. Si la respuesta no era JSON (el resumen en
    Markdown, por ejemplo), se imprime tal cual.
    """
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))


def _read_descriptor(path: Path) -> Descriptor:
    """Lee el fichero YAML que se va a subir en el multipart.

    El nombre importa: viaja en el `filename` del multipart y es lo que el
    servidor registra. Un fichero que no se puede leer es un error de USO, no de
    la API: se detecta antes de abrir ninguna conexion.
    """
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise _UsageError(f"no se pudo leer el descriptor '{path}': {exc.strerror or exc}")
    if not content.strip():
        raise _UsageError(f"el descriptor '{path}' esta vacio")
    return Descriptor(filename=path.name, content=content)


class _UsageError(Exception):
    """Error atribuible a como se invoco el CLI, no a la API. Sale con `EXIT_USAGE`."""


def _resolve_api_key(explicit: str | None) -> str:
    """API key del argumento, del entorno o error de uso.

    Se comprueba aqui y no al recibir un 401 para no gastar una peticion —y una
    espera de fase, en el caso de `run`— en algo que ya se sabe que va a fallar.
    """
    key = explicit or os.environ.get(ENV_API_KEY) or os.environ.get(ENV_API_KEY_FALLBACK)
    if not key:
        raise _UsageError(
            "falta la API key: pasala con --api-key o exportala en "
            f"${ENV_API_KEY} (o ${ENV_API_KEY_FALLBACK})"
        )
    return key


def _client_from(args: argparse.Namespace) -> ApiClient:
    """Construye el cliente HTTP a partir de los argumentos y del entorno."""
    base_url = args.base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL
    return ApiClient(base_url=base_url, api_key=_resolve_api_key(args.api_key))


def _report_phase(result: PhaseResult, *, partial_hint: str) -> int:
    """Imprime el desenlace de una fase y lo traduce a codigo de salida.

    Las tres ordenes que disparan una fase (`run`, `elcm`, `rm`) comparten esto:
    el cuerpo va a stdout y el codigo HTTP decide el desenlace. El aviso del 207
    va a stderr para no ensuciar el JSON de stdout, que es lo que se canaliza.
    """
    _print_json(result.payload)
    if result.status_code == 207:
        print(f"AVISO: completado parcialmente (HTTP 207). {partial_hint}", file=sys.stderr)
        return EXIT_PARTIAL
    return EXIT_OK


# ===== Subcomandos =====


def _cmd_run(args: argparse.Namespace, client: ApiClient) -> int:
    """POST /executions con el descriptor como fichero subido."""
    descriptor = _read_descriptor(args.descriptor)
    result = client.create_execution(descriptor, wait=args.wait)
    return _report_phase(
        result,
        partial_hint=(
            "la Trial Network esta desplegada pero el tunel WireGuard hay que "
            "montarlo a mano (vpn_status=MANUAL_REQUIRED); el experimento ELCM "
            "no funcionara hasta entonces."
        ),
    )


def _cmd_elcm(args: argparse.Namespace, client: ApiClient) -> int:
    """POST /executions/{id}/elcm sobre una infraestructura que ya existe."""
    descriptor = _read_descriptor(args.descriptor)
    result = client.start_elcm(args.execution_id, descriptor, wait=args.wait)
    return _report_phase(
        result,
        partial_hint="el experimento termino pero el dataset quedo a medias; revisa el resumen.",
    )


def _cmd_status(args: argparse.Namespace, client: ApiClient) -> int:
    """GET /executions/{id}: estado resumido."""
    _print_json(client.get_execution(args.execution_id))
    return EXIT_OK


def _cmd_detail(args: argparse.Namespace, client: ApiClient) -> int:
    """GET /executions/{id}/detail: el registro completo."""
    _print_json(client.get_execution_detail(args.execution_id))
    return EXIT_OK


def _cmd_summary(args: argparse.Namespace, client: ApiClient) -> int:
    """GET /executions/{id}/summary, en JSON o en Markdown."""
    _print_json(client.get_execution_summary(args.execution_id, as_markdown=args.format == "md"))
    return EXIT_OK


def _cmd_download(args: argparse.Namespace, client: ApiClient) -> int:
    """GET /executions/{id}/download: el ZIP con todo lo que dejo la ejecucion."""
    content = client.download_execution(args.execution_id, secrets=args.secrets)
    target = args.output or Path(f"{args.execution_id}.zip")
    try:
        target.write_bytes(content)
    except OSError as exc:
        raise _UsageError(f"no se pudo escribir '{target}': {exc.strerror or exc}")
    # A stderr: stdout queda libre por coherencia con el resto de ordenes, aunque
    # aqui el contenido vaya a un fichero.
    print(f"{target} ({len(content) / 1024:.1f} KiB)", file=sys.stderr)
    return EXIT_OK


def _cmd_ls(args: argparse.Namespace, client: ApiClient) -> int:
    """GET /executions: que TN hay y cual tiene el tunel arriba."""
    _print_json(client.list_executions())
    return EXIT_OK


def _cmd_pause(args: argparse.Namespace, client: ApiClient) -> int:
    """POST /executions/{id}/pause: aparta la TN bajando su tunel."""
    return _report_phase(
        client.pause_tn(args.execution_id),
        partial_hint=(
            "la ejecucion queda pausada pero el tunel WireGuard no se pudo bajar "
            "(vpn_status=DOWN_ERROR); compruebalo antes de conectar otra TN."
        ),
    )


def _cmd_resume(args: argparse.Namespace, client: ApiClient) -> int:
    """POST /executions/{id}/resume: vuelve a conectar con una TN pausada."""
    return _report_phase(
        client.resume_tn(args.execution_id),
        partial_hint=(
            "la TN se recupero pero el tunel WireGuard hay que montarlo a mano "
            "(vpn_status=MANUAL_REQUIRED); el experimento ELCM no funcionara hasta entonces."
        ),
    )


def _cmd_rm(args: argparse.Namespace, client: ApiClient) -> int:
    """DELETE /executions/{id}/tn: borra la Trial Network y espera a la purga."""
    return _report_phase(
        client.delete_tn(args.execution_id),
        partial_hint="la TN se borro pero algo quedo sin limpiar; revisa el detalle.",
    )


# ===== Parser =====


def build_parser() -> argparse.ArgumentParser:
    """Arbol de subcomandos. Se construye aparte para poder probarlo sin ejecutar nada."""
    parser = argparse.ArgumentParser(
        prog="daas",
        description=(
            "Cliente de consola del DaaS Orchestrator: lanza ejecuciones, consulta su "
            "estado y recoge los resultados hablando con la API por HTTP."
        ),
        epilog=(
            f"Variables de entorno: ${ENV_BASE_URL} (por defecto {DEFAULT_BASE_URL}) y "
            f"${ENV_API_KEY} o ${ENV_API_KEY_FALLBACK}. "
            "Codigos de salida: 0 correcto, 1 error de API, 2 error de uso, 3 parcial (207)."
        ),
    )

    # Comunes a todos los subcomandos. Van en un padre y no en el parser raiz
    # para que se puedan escribir DESPUES del subcomando, que es como se teclean:
    # `daas status tn-1 --base-url ...` y no `daas --base-url ... status tn-1`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--base-url",
        metavar="URL",
        help=f"URL de la API (por defecto ${ENV_BASE_URL} o {DEFAULT_BASE_URL})",
    )
    common.add_argument(
        "--api-key",
        metavar="KEY",
        help=f"API key (por defecto ${ENV_API_KEY} o ${ENV_API_KEY_FALLBACK})",
    )

    subcommands = parser.add_subparsers(dest="command", metavar="ORDEN")
    subcommands.required = True

    run = subcommands.add_parser(
        "run",
        parents=[common],
        help="lanza una ejecucion desde un Dataset Descriptor",
        description=(
            "Sube el descriptor a POST /executions. Por defecto espera a que la VPN "
            "quede resuelta (hasta 40 min); con --no-wait devuelve 202 al instante y "
            "el despliegue sigue por detras."
        ),
    )
    run.add_argument("descriptor", type=Path, help="fichero YAML del Dataset Descriptor")
    run.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="esperar a que termine la fase (por defecto: si)",
    )
    run.set_defaults(func=_cmd_run)

    elcm = subcommands.add_parser(
        "elcm",
        parents=[common],
        help="lanza un experimento ELCM sobre una ejecucion existente",
        description=(
            "Sube el cuerpo a POST /executions/{id}/elcm. Solo lleva `experiment` y "
            "`dataset`: la infraestructura ya existe y no se vuelve a describir."
        ),
    )
    elcm.add_argument("execution_id", metavar="ID", help="identificador de la ejecucion")
    elcm.add_argument("descriptor", type=Path, help="fichero YAML con experiment y dataset")
    elcm.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="esperar a que el dataset este recolectado (por defecto: si)",
    )
    elcm.set_defaults(func=_cmd_elcm)

    status = subcommands.add_parser(
        "status", parents=[common], help="estado resumido de una ejecucion"
    )
    status.add_argument("execution_id", metavar="ID", help="identificador de la ejecucion")
    status.set_defaults(func=_cmd_status)

    detail = subcommands.add_parser(
        "detail", parents=[common], help="registro completo de una ejecucion"
    )
    detail.add_argument("execution_id", metavar="ID", help="identificador de la ejecucion")
    detail.set_defaults(func=_cmd_detail)

    summary = subcommands.add_parser(
        "summary",
        parents=[common],
        help="resumen legible: pasos, duraciones y resultados",
        description=(
            "Se construye en vivo, asi que puede consultarse mientras la ejecucion "
            "sigue en curso."
        ),
    )
    summary.add_argument("execution_id", metavar="ID", help="identificador de la ejecucion")
    summary.add_argument(
        "--format",
        choices=("json", "md"),
        default="json",
        help="json (por defecto) o md para el informe en Markdown",
    )
    summary.set_defaults(func=_cmd_summary)

    download = subcommands.add_parser(
        "download",
        parents=[common],
        help="descarga en ZIP todo lo que dejo la ejecucion",
        description=(
            "Por defecto NO incluye los ficheros con claves de acceso (la config de "
            "WireGuard y los informes crudos de TNLCM); --secrets los incluye, y el ZIP "
            "deja entonces de ser compartible sin revisarlo."
        ),
    )
    download.add_argument("execution_id", metavar="ID", help="identificador de la ejecucion")
    download.add_argument(
        "--secrets",
        action="store_true",
        help="incluir tambien los ficheros con claves de acceso",
    )
    download.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="FICHERO",
        help="donde escribir el ZIP (por defecto <ID>.zip)",
    )
    download.set_defaults(func=_cmd_download)

    listing = subcommands.add_parser(
        "ls",
        parents=[common],
        help="lista las ejecuciones conocidas y su estado de conexion",
        description="`vpn_status=UP` marca la TN que tiene el tunel levantado ahora mismo.",
    )
    listing.set_defaults(func=_cmd_ls)

    pause = subcommands.add_parser(
        "pause",
        parents=[common],
        help="aparta una TN sin borrarla (baja su tunel WireGuard)",
        description=(
            "La TN sigue viva en TNLCM: solo se baja el tunel para poder conectar "
            "otra. Se vuelve con `daas resume`, sin redesplegar."
        ),
    )
    pause.add_argument("execution_id", metavar="ID", help="identificador de la ejecucion")
    pause.set_defaults(func=_cmd_pause)

    resume = subcommands.add_parser(
        "resume",
        parents=[common],
        help="vuelve a conectar con una TN pausada",
        description=(
            "Reabre el tunel de una TN que sigue desplegada. No lleva descriptor: "
            "el original y los experimentos ya ejecutados se conservan."
        ),
    )
    resume.add_argument("execution_id", metavar="ID", help="identificador de la ejecucion")
    resume.set_defaults(func=_cmd_resume)

    remove = subcommands.add_parser(
        "rm",
        parents=[common],
        help="borra la Trial Network de una ejecucion",
        description="Espera a que la TN quede purgada (hasta 50 min).",
    )
    remove.add_argument("execution_id", metavar="ID", help="identificador de la ejecucion")
    remove.set_defaults(func=_cmd_rm)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada de consola (`daas`). Devuelve el codigo de salida.

    Todo el manejo de errores esta aqui y no en los subcomandos: cada `_cmd_*`
    hace su llamada y deja que la excepcion suba, que es el fail-fast del
    proyecto. Aqui se traduce a un mensaje en stderr y a un codigo.
    """
    args = build_parser().parse_args(argv)
    _force_utf8_output()

    try:
        return args.func(args, _client_from(args))
    except _UsageError as exc:
        print(f"daas: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ApiError as exc:
        print(f"daas: {exc.message}", file=sys.stderr)
        if isinstance(exc.detail, (dict, list)):
            print(json.dumps(exc.detail, indent=2, ensure_ascii=False), file=sys.stderr)
        return EXIT_API_ERROR
    except KeyboardInterrupt:
        # Una fase puede tardar 70 min y cortarla con Ctrl-C es normal. Se avisa
        # de que el servidor NO se entera: sigue trabajando por detras.
        print(
            "\ndaas: interrumpido. La operacion puede seguir en curso en el servidor.",
            file=sys.stderr,
        )
        return EXIT_API_ERROR


if __name__ == "__main__":
    sys.exit(main())
