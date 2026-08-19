"""El CLI tiene que pedir lo que dice pedir, y salir con el codigo correcto.

Son pruebas de nivel `unit`: no levantan el servicio ni salen a la red. La
frontera simulada es el TRANSPORTE de httpx (fixture `fake_http`), asi que corre
httpx de verdad —construccion de la URL, cabeceras, codificacion multipart— y lo
que se afirma es el contrato de cable con la API, que no cambia al mover codigo.

Que la fixture funcione aqui depende de que `app/client.py` abra un
`httpx.Client` explicito. Con la funcion de modulo `httpx.request(...)` que usaba
antes, el `MockTransport` NO se aplicaba —`httpx.request` resuelve `Client` desde
los globals de `httpx._api`— y estas pruebas habrian salido a la red de verdad
sin fallar ni avisar, que es la peor forma de pasar.
"""

import json

import pytest

from app import cli

pytestmark = pytest.mark.usefixtures("fake_http")


@pytest.fixture
def descriptor_file(tmp_path):
    """Un descriptor minimo en disco, que es como el CLI lo recibe siempre."""
    path = tmp_path / "descriptor.yaml"
    path.write_text("infrastructure:\n  name: tn-x\n", encoding="utf-8")
    return path


def _run(argv, **env):
    """Ejecuta el CLI con la API key ya puesta, salvo que la prueba diga otra cosa."""
    return cli.main([*argv, "--api-key", env.get("api_key", "secreta")])


# ===== Parseo de argumentos =====


@pytest.mark.parametrize(
    ("argv", "handler"),
    [
        (["run", "d.yaml"], "_cmd_run"),
        (["elcm", "tn-1", "d.yaml"], "_cmd_elcm"),
        (["status", "tn-1"], "_cmd_status"),
        (["detail", "tn-1"], "_cmd_detail"),
        (["summary", "tn-1"], "_cmd_summary"),
        (["download", "tn-1"], "_cmd_download"),
        (["rm", "tn-1"], "_cmd_rm"),
    ],
)
def test_cada_orden_despacha_a_su_handler(argv, handler):
    """Las siete ordenes existen y llevan a la funcion que les toca.

    Un `set_defaults` copiado y no editado —el fallo tipico al anadir un
    subcomando— manda dos ordenes al mismo sitio y aqui se ve.
    """
    args = cli.build_parser().parse_args(argv)
    assert args.func is getattr(cli, handler)


@pytest.mark.parametrize(
    ("argv", "esperado"),
    [
        (["run", "d.yaml"], True),
        (["run", "d.yaml", "--wait"], True),
        (["run", "d.yaml", "--no-wait"], False),
        (["elcm", "tn-1", "d.yaml", "--no-wait"], False),
    ],
)
def test_wait_se_parsea_a_booleano(argv, esperado):
    """`--wait/--no-wait` decide si la peticion bloquea; por defecto, si."""
    assert cli.build_parser().parse_args(argv).wait is esperado


def test_opciones_comunes_se_admiten_despues_del_subcomando():
    """Es como se teclean: `daas status tn-1 --base-url ...`, no al reves."""
    args = cli.build_parser().parse_args(
        ["status", "tn-1", "--base-url", "http://x:9", "--api-key", "k"]
    )
    assert (args.execution_id, args.base_url, args.api_key) == ("tn-1", "http://x:9", "k")


def test_summary_format_por_defecto_es_json():
    assert cli.build_parser().parse_args(["summary", "tn-1"]).format == "json"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["run"],
        ["elcm", "solo-el-id"],
        ["status"],
        ["orden-que-no-existe"],
    ],
)
def test_invocaciones_incompletas_salen_con_codigo_de_uso(argv):
    """argparse ya sale con 2, que es el mismo codigo que usa el CLI para uso."""
    with pytest.raises(SystemExit) as salida:
        cli.build_parser().parse_args(argv)
    assert salida.value.code == cli.EXIT_USAGE


# ===== --help =====


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["run"],
        ["elcm"],
        ["status"],
        ["detail"],
        ["summary"],
        ["download"],
        ["rm"],
    ],
)
def test_help_de_cada_orden_no_revienta(argv, capsys):
    """Un subparser mal montado o un `%` suelto en el help solo se ve al pintarlo."""
    with pytest.raises(SystemExit) as salida:
        cli.build_parser().parse_args([*argv, "--help"])
    assert salida.value.code == 0
    assert capsys.readouterr().out.startswith("usage: daas")


# ===== Comando -> endpoint =====


def test_status_pide_la_ejecucion(fake_http, capsys):
    fake_http.respond(200, json={"execution_id": "tn-1", "state": "COMPLETED"})

    assert _run(["status", "tn-1"]) == cli.EXIT_OK
    assert fake_http.paths_for("GET") == ["/executions/tn-1"]
    assert json.loads(capsys.readouterr().out) == {
        "execution_id": "tn-1",
        "state": "COMPLETED",
    }


def test_detail_pide_el_registro_completo(fake_http):
    fake_http.respond(200, json={"execution_id": "tn-1"})

    assert _run(["detail", "tn-1"]) == cli.EXIT_OK
    assert fake_http.paths_for("GET") == ["/executions/tn-1/detail"]


def test_summary_json_no_pide_markdown(fake_http):
    fake_http.respond(200, json={"steps": []})

    assert _run(["summary", "tn-1"]) == cli.EXIT_OK
    assert fake_http.last.url.path == "/executions/tn-1/summary"
    assert "format" not in fake_http.last.url.params


def test_summary_md_pide_markdown_y_lo_imprime_tal_cual(fake_http, capsys):
    """El informe es texto: pasarlo por json.dumps lo llenaria de \\n escapados."""
    fake_http.respond(200, text="# Resumen\n\nTodo bien.")

    assert _run(["summary", "tn-1", "--format", "md"]) == cli.EXIT_OK
    assert fake_http.last.url.params["format"] == "markdown"
    assert capsys.readouterr().out.startswith("# Resumen")


def test_summary_md_con_emoji_sale_entero(fake_http, capsys):
    """El informe del servidor trae ❌ y ✅, y tienen que llegar a stdout."""
    fake_http.respond(200, text="# Resumen\n\n- **Status:** ❌ Failed\n")

    assert _run(["summary", "tn-1", "--format", "md"]) == cli.EXIT_OK
    assert "❌ Failed" in capsys.readouterr().out


def test_summary_md_con_emoji_sobrevive_a_un_stdout_ascii(fake_http, monkeypatch, tmp_path):
    """La salida no siempre admite lo que el servidor manda, y `print` no perdona.

    Reproduce el flujo real: stdout en ASCII. Pasa en Windows (consola cp1252) y
    en Linux con `LANG=C`, que es el caso por defecto de muchas imagenes de
    contenedor, de `cron` y de las unidades de systemd. Sin el arreglo, `print`
    levanta `UnicodeEncodeError` y la orden muere con un traceback.

    No vale hacerlo con `capsys`: la captura de pytest ya es UTF-8, asi que ahi
    el fallo no se reproduce y la prueba pasaria sin comprobar nada.
    """
    destino = tmp_path / "salida.txt"
    fake_http.respond(200, text="# Resumen\n\n- **Status:** ❌ Failed\n")

    with open(destino, "w", encoding="ascii") as ascii_stdout:
        monkeypatch.setattr(cli.sys, "stdout", ascii_stdout)
        codigo = _run(["summary", "tn-1", "--format", "md"])

    assert codigo == cli.EXIT_OK
    assert "❌ Failed" in destino.read_text(encoding="utf-8")


def test_forzar_utf8_tolera_un_stdout_sin_reconfigure(monkeypatch):
    """Bajo la captura de pytest, stdout no es un `TextIOWrapper` y no lo tiene."""

    class SinReconfigure:
        pass

    monkeypatch.setattr(cli.sys, "stdout", SinReconfigure())
    monkeypatch.setattr(cli.sys, "stderr", SinReconfigure())
    cli._force_utf8_output()  # no debe levantar


def test_rm_borra_la_trial_network(fake_http):
    fake_http.respond(200, json={"state": "DELETED"})

    assert _run(["rm", "tn-1"]) == cli.EXIT_OK
    assert fake_http.paths_for("DELETE") == ["/executions/tn-1/tn"]


def test_la_api_key_viaja_en_la_cabecera(fake_http):
    fake_http.respond(200, json={})

    _run(["status", "tn-1"])
    assert fake_http.last.headers["x-api-key"] == "secreta"


def test_base_url_se_respeta(fake_http):
    fake_http.respond(200, json={})

    _run(["status", "tn-1", "--base-url", "http://otro-host:9999"])
    assert str(fake_http.last.url).startswith("http://otro-host:9999/")


def test_base_url_sale_del_entorno_si_no_se_pasa(fake_http, monkeypatch):
    monkeypatch.setenv(cli.ENV_BASE_URL, "http://del-entorno:1234")
    fake_http.respond(200, json={})

    _run(["status", "tn-1"])
    assert str(fake_http.last.url).startswith("http://del-entorno:1234/")


# ===== Subida del descriptor =====


def test_run_sube_el_descriptor_como_fichero(fake_http, descriptor_file):
    """El campo y el nombre del fichero son el contrato con `api/body_formats.py`."""
    fake_http.respond(200, json={"execution_id": "tn-x"})

    assert _run(["run", str(descriptor_file)]) == cli.EXIT_OK
    assert fake_http.paths_for("POST") == ["/executions"]

    campos = fake_http.multipart()
    filename, contenido = campos["descriptor"]
    assert filename == "descriptor.yaml"
    assert "name: tn-x" in contenido


def test_run_espera_por_defecto_y_no_esperar_se_pide_explicito(fake_http, descriptor_file):
    fake_http.respond(200, json={})

    _run(["run", str(descriptor_file)])
    assert fake_http.last.url.params["wait"] == "true"

    _run(["run", str(descriptor_file), "--no-wait"])
    assert fake_http.last.url.params["wait"] == "false"


def test_elcm_sube_el_experimento_a_la_ejecucion(fake_http, descriptor_file):
    fake_http.respond(200, json={})

    assert _run(["elcm", "tn-1", str(descriptor_file)]) == cli.EXIT_OK
    assert fake_http.paths_for("POST") == ["/executions/tn-1/elcm"]
    assert "descriptor" in fake_http.multipart()


def test_descriptor_inexistente_es_error_de_uso_y_no_llega_a_pedir_nada(
    fake_http, tmp_path, capsys
):
    """Fail-fast: no se abre conexion para algo que ya se sabe que no se puede enviar."""
    assert _run(["run", str(tmp_path / "no-existe.yaml")]) == cli.EXIT_USAGE
    assert fake_http.requests == []
    assert "no se pudo leer el descriptor" in capsys.readouterr().err


def test_descriptor_vacio_es_error_de_uso(fake_http, tmp_path, capsys):
    vacio = tmp_path / "vacio.yaml"
    vacio.write_text("   \n", encoding="utf-8")

    assert _run(["run", str(vacio)]) == cli.EXIT_USAGE
    assert fake_http.requests == []
    assert "esta vacio" in capsys.readouterr().err


# ===== Descarga =====


def test_download_escribe_el_zip(fake_http, tmp_path):
    zip_bytes = b"PK\x03\x04doble-cero-y-algo-mas"
    fake_http.respond(200, content=zip_bytes)
    destino = tmp_path / "salida.zip"

    assert _run(["download", "tn-1", "-o", str(destino)]) == cli.EXIT_OK
    assert fake_http.last.url.path == "/executions/tn-1/download"
    assert fake_http.last.url.params["secrets"] == "false"
    assert destino.read_bytes() == zip_bytes


def test_download_con_secrets_los_pide(fake_http, tmp_path):
    fake_http.respond(200, content=b"PK")

    _run(["download", "tn-1", "--secrets", "-o", str(tmp_path / "s.zip")])
    assert fake_http.last.url.params["secrets"] == "true"


def test_download_sin_o_usa_el_id_como_nombre(fake_http, tmp_path, monkeypatch):
    fake_http.respond(200, content=b"PK")
    monkeypatch.chdir(tmp_path)

    assert _run(["download", "tn-1"]) == cli.EXIT_OK
    assert (tmp_path / "tn-1.zip").read_bytes() == b"PK"


def test_download_no_ensucia_stdout(fake_http, tmp_path, capsys):
    """stdout es lo que se canaliza a un fichero o a jq: el aviso va a stderr."""
    fake_http.respond(200, content=b"PK")

    _run(["download", "tn-1", "-o", str(tmp_path / "s.zip")])
    salida = capsys.readouterr()
    assert salida.out == ""
    assert "KiB" in salida.err


# ===== Codigos de salida y traduccion de errores =====


def test_207_sale_con_codigo_propio(fake_http, descriptor_file, capsys):
    """207 no es exito: sin tunel, el `daas elcm` de la cadena fallaria."""
    fake_http.respond(207, json={"vpn_status": "MANUAL_REQUIRED"})

    assert _run(["run", str(descriptor_file)]) == cli.EXIT_PARTIAL
    salida = capsys.readouterr()
    assert json.loads(salida.out) == {"vpn_status": "MANUAL_REQUIRED"}
    assert "parcialmente" in salida.err


def test_404_sale_con_error_de_api(fake_http, capsys):
    fake_http.respond(404, json={"detail": "Ejecucion no encontrada"})

    assert _run(["status", "tn-fantasma"]) == cli.EXIT_API_ERROR
    assert "Ejecucion no encontrada" in capsys.readouterr().err


def test_401_se_traduce_a_algo_accionable(fake_http, capsys):
    fake_http.respond(401, json={"detail": "API key invalida"})

    assert _run(["status", "tn-1"]) == cli.EXIT_API_ERROR
    assert "API key" in capsys.readouterr().err


def test_422_de_pydantic_se_lee_como_una_frase(fake_http, descriptor_file, capsys):
    """El 422 crudo es una lista de dicts con `loc`; asi no se puede leer en consola."""
    fake_http.respond(
        422,
        json={
            "detail": [
                {"loc": ["body", "dataset", "output", 0], "msg": "valor no permitido"},
            ]
        },
    )

    assert _run(["run", str(descriptor_file)]) == cli.EXIT_API_ERROR
    error = capsys.readouterr().err
    assert "dataset.output.0: valor no permitido" in error


def test_sin_api_key_no_se_intenta_la_peticion(fake_http, monkeypatch, capsys):
    """Gastar una espera de fase para acabar en 401 no tiene sentido."""
    monkeypatch.delenv(cli.ENV_API_KEY, raising=False)
    monkeypatch.delenv(cli.ENV_API_KEY_FALLBACK, raising=False)

    assert cli.main(["status", "tn-1"]) == cli.EXIT_USAGE
    assert fake_http.requests == []
    assert "falta la API key" in capsys.readouterr().err


def test_la_api_key_puede_venir_del_entorno(fake_http, monkeypatch):
    monkeypatch.delenv(cli.ENV_API_KEY, raising=False)
    monkeypatch.setenv(cli.ENV_API_KEY_FALLBACK, "la-del-env")
    fake_http.respond(200, json={})

    assert cli.main(["status", "tn-1"]) == cli.EXIT_OK
    assert fake_http.last.headers["x-api-key"] == "la-del-env"
