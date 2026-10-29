"""Connection and DownloadClient behavior tests with mocked HTTP transport."""

import httpx
import pytest

from sapdlm.client.connection import (
    AuthenticationConfigError,
    ConnectionConfig,
    InvalidProtocolError,
    SapConnection,
    SapIOError,
)
from sapdlm.client.download_client import DownloadClient, ItemNotFoundError


def make_connection(config: ConnectionConfig, handler) -> SapConnection:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SapConnection(config, client=client)


def test_https_enforced():
    connection = make_connection(
        ConnectionConfig(sap_url="http://example.com", username="u", password="p"),
        lambda request: httpx.Response(200, text=""),
    )
    with pytest.raises(InvalidProtocolError):
        connection.get("/odata/svt/swdcuisrv/DownloadContentSet?_MODE=BASKET_CONTENT")


def test_basic_auth_validation_missing_password():
    config = ConnectionConfig(sap_url="https://portal", username="u", password="")
    with pytest.raises(AuthenticationConfigError, match="HTTP/1.1 401"):
        config.validate_auth()


def test_cert_auth_validation_missing_path():
    config = ConnectionConfig(sap_url="https://portal", use_cert_auth=True)
    with pytest.raises(AuthenticationConfigError):
        config.validate_auth()


def test_401_aborts_without_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401)

    connection = make_connection(
        ConnectionConfig(sap_url="https://portal", username="u", password="p"), handler
    )
    with pytest.raises(SapIOError, match="HTTP/1.1 401"):
        connection.get("/x")
    assert calls["n"] == 1


def test_get_with_retries_recovers():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, text="VERSION=3.2.1\n")

    connection = make_connection(
        ConnectionConfig(sap_url="https://portal", username="u", password="p"), handler
    )
    response = connection.get_with_retries("/x")
    assert calls["n"] == 3
    assert response.status_code == 200


def test_get_with_retries_exhausts():
    def handler(request):
        return httpx.Response(503)

    connection = make_connection(
        ConnectionConfig(sap_url="https://portal", username="u", password="p"), handler
    )
    with pytest.raises(SapIOError):
        connection.get_with_retries("/x", attempts=5)


def test_content_length_prefers_content_range():
    response = httpx.Response(
        206,
        headers={"Content-Length": "100", "Content-Range": "bytes 0-99/12345"},
    )
    from sapdlm.client.connection import SapResponse

    wrapped = SapResponse(status_code=206, headers=response.headers, body=b"")
    assert wrapped.content_length() == 12345


# --------------------------------------------------------------- DownloadClient

BASKET_BODY = '{"d":{"results":[{"Name":"OBJID","Value":"K1|SAR|Text|10|1|20250101000000|0"}]}}'


def test_check_basket_content_success():
    connection = make_connection(
        ConnectionConfig(sap_url="https://portal", username="u", password="p"),
        lambda request: httpx.Response(200, text=BASKET_BODY),
    )
    client = DownloadClient(connection.config)
    client._test_connection = connection

    from unittest.mock import patch

    with patch("sapdlm.client.download_client.SapConnection", return_value=connection):
        basket = client.check_basket_content()
    assert client.connected is True
    assert basket.items["K1"].size_kb == 10
    assert client.busy is False


def test_remove_from_basket_unknown_item():
    client = DownloadClient(ConnectionConfig(sap_url="https://portal", username="u", password="p"))
    client.basket = None
    with pytest.raises(ItemNotFoundError):
        client.remove_from_basket("NOPE")


def test_check_basket_content_auth_failure():
    connection = make_connection(
        ConnectionConfig(sap_url="https://portal", username="u", password="p"),
        lambda request: httpx.Response(401),
    )
    client = DownloadClient(connection.config)
    from unittest.mock import patch

    with (
        patch("sapdlm.client.download_client.SapConnection", return_value=connection),
        pytest.raises(SapIOError, match="401"),
    ):
        client.check_basket_content()
    assert client.connected is False
