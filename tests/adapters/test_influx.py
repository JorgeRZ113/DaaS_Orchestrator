"""Contrato HTTP de la consulta cruda a InfluxDB v2.

El token es un secreto: aqui se comprueba que viaja en la cabecera `Authorization`
y en ningun otro sitio de la peticion (nunca en la URL, §8.7 de las reglas del
proyecto).
"""

import pytest

from app.adapters import influx


@pytest.mark.asyncio
async def test_query_flux_csv_posts_flux_with_token_in_header(fake_http):
    fake_http.respond(200, text="csv-body")

    text = await influx.query_flux_csv(
        host="192.168.199.2", port=8086, org="testing", token="secret-token", flux="from(...)"
    )

    assert text == "csv-body"

    request = fake_http.last
    assert request.method == "POST"
    assert str(request.url) == "http://192.168.199.2:8086/api/v2/query?org=testing"
    assert request.headers["Authorization"] == "Token secret-token"
    assert request.headers["Content-Type"] == "application/vnd.flux"
    assert request.content.decode() == "from(...)"
    # El secreto no puede acabar en la query string (quedaria en logs y proxies).
    assert "secret-token" not in str(request.url)
