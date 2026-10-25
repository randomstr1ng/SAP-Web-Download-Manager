"""Parser tests for both endpoint styles - fixtures derived from the original
client logic (see docs/PROTOCOL.md)."""

import pytest

from sapdlm.client.basket import ResponseParseError
from sapdlm.client.constants import BasketStatus, GlobalStatus
from sapdlm.client.endpoints import EndpointStyle
from sapdlm.client.parsers import (
    is_valid_download_url,
    parse_basket_content,
    parse_download_end,
    parse_download_start,
    parse_object_version,
)

PORTAL = EndpointStyle.PORTAL
MARKETPLACE = EndpointStyle.MARKETPLACE

# ------------------------------------------------------------ basket content (portal)

PORTAL_BASKET = """{
  "d": {
    "results": [
      {"Name": "USERNAME1", "Value": "Jane"},
      {"Name": "USERNAME2", "Value": "Doe"},
      {"Name": "OBJCNT", "Value": "2"},
      {"Name": "OBJID", "Value": "0012345678|SAR|NetWeaver 7.5 kernel|512000|1|20241129120000|0"},
      {"Name": "OBJID", "Value": "0087654321|ZIP|Hostagent 7.21|2048|2|20250102080500|0"},
      {"Name": "DLM_UPG_VERSION", "Value": "3.3.0"},
      {"Name": "DLM_UPG_URL", "Value": "https://tools.hana.ondemand.com/dlm"}
    ]
  }
}"""


def test_portal_basket_items():
    content = parse_basket_content(PORTAL_BASKET, PORTAL)
    assert content.full_name == "Jane Doe"
    assert len(content.items) == 2
    item = content.items["0012345678"]
    assert item.doc_type == "SAR"
    assert item.text == "NetWeaver 7.5 kernel"
    assert item.size_kb == 512000
    assert item.basket_status == BasketStatus.NEW
    assert item.reg_date == "20241129"
    assert item.reg_time == "120000"
    assert item.global_status == GlobalStatus.OK
    assert item.size_text == "512,000 KB"
    assert content.upgrade.version == "3.3.0"


def test_portal_basket_error():
    body = '{"d":{"results":[{"Name":"ERRORCODE","Value":"E123"},{"Name":"ERRORINFO","Value":"boom"}]}}'
    with pytest.raises(ResponseParseError, match=r"\[E123\] boom"):
        parse_basket_content(body, PORTAL)


# ------------------------------------------------------ basket content (marketplace)

MARKETPLACE_BASKET = """VERSION=3.2.1
!USER_IP=10.1.2.3
USERNAME1=Jane
USERNAME2=Doe
SAPPORT=443
SERVER=bharat.wdf.sap.corp
OBJCNT=1
DOCKEY=0012345678
DOCTYPE=SAR
TEXT=NetWeaver 7.5 kernel
SIZE=512000
BASKETSTATUS=1
REGDATE=20241129
REGTIME=120000
GLOBALSTATUS=0
"""


def test_marketplace_basket_items():
    content = parse_basket_content(MARKETPLACE_BASKET, MARKETPLACE)
    assert content.version == "3.2.1"
    assert content.user_ip == "10.1.2.3"
    assert content.servers == ["bharat.wdf.sap.corp"]
    assert content.full_name == "Jane Doe"
    item = content.items["0012345678"]
    assert item.doc_type == "SAR"
    assert item.size_kb == 512000
    assert item.reg_datetime_key == "20241129120000"


def test_marketplace_invalid_line():
    with pytest.raises(ResponseParseError, match="Invalid line"):
        parse_basket_content("BADLINE\nVERSION=1\n", MARKETPLACE)


def test_marketplace_attribute_without_item():
    with pytest.raises(ResponseParseError, match="without Info Object"):
        parse_basket_content("SIZE=10\n", MARKETPLACE)


# ------------------------------------------------------------------- object version

def test_object_version_portal():
    body = '{"d":{"results":[{"Name":"GLOBALSTATUS","Value":"0"},{"Name":"INFOHDVERSION","Value":"17"}]}}'
    info = parse_object_version(body, PORTAL)
    assert info.global_status == GlobalStatus.OK
    assert info.reported_version == 17


def test_object_version_portal_not_authorized():
    body = (
        '{"d":{"results":[{"Name":"ERRORCODE","Value":"NOT_AUTHORIZED"},'
        '{"Name":"ERRORINFO","Value":"no access"}]}}'
    )
    with pytest.raises(ResponseParseError, match="NOT_AUTHORIZED"):
        parse_object_version(body, PORTAL)


def test_object_version_marketplace():
    info = parse_object_version("GLOBALSTATUS=0\nINFOHDVERSION=12\n", MARKETPLACE)
    assert info.reported_version == 12
    with pytest.raises(ResponseParseError, match=r"\[E9\] bad"):
        parse_object_version("ERRORCODE=E9\nERRORINFO=bad\n", MARKETPLACE)


# ------------------------------------------------------------------ download start

PORTAL_START = """{"d":{"results":[
  {"Name": "DOCKEY", "Value": "0012345678"},
  {"Name": "DOWNLOAD0", "Value": "https://ldciotap1.wdf.sap.corp/file0.sar"},
  {"Name": "TOKEN_SAPSSO", "Value": "abc123"},
  {"Name": "DOWNLOAD1", "Value": "https://cdn.akamai.edgesuite.net/file1.sar"},
  {"Name": "DESCRIPTION", "Value": "SAP Kernel 7.49"},
  {"Name": "DESCRIPTION", "Value": "rowstartPLT 640rowstart"},
  {"Name": "DESCRIPTION_CONVERSION", "Value": "BINARY_PATCHES"}
]}}"""


def test_download_start_portal():
    start = parse_download_start(PORTAL_START, PORTAL)
    assert start.dockey == "0012345678"
    assert len(start.components) == 2
    assert start.components[0].url.endswith("file0.sar")
    assert start.components[0].token == "SAPSSO=abc123"
    assert start.components[1].token == ""  # token attaches to most recent DOWNLOAD
    assert start.description_conversion == "BINARY_PATCHES"
    assert start.description[0].startswith("{1F3AAA90-4782-4632-8C5E-E92F7A7751E4}=")


def test_download_start_marketplace():
    body = (
        "OBJID=0012345678\n"
        "DOWNLOAD0=https://ldciotap1.wdf.sap.corp/file0.sar\n"
        "TOKEN_SAPSSO=abc123\n"
        "DESCRIPTION=Kernel\n"
    )
    start = parse_download_start(body, MARKETPLACE)
    assert start.dockey == "0012345678"
    assert start.components[0].name == "DOWNLOAD0"
    assert start.components[0].token == "SAPSSO=abc123"
    assert start.description == ["Kernel"]


# -------------------------------------------------------------------- download end

def test_download_end_portal_ok():
    body = '{"d":{"results":[{"Name":"BASKETSTATUS","Value":"4"}]}}'
    assert parse_download_end(body, PORTAL).error is False


def test_download_end_portal_failure():
    body = '{"d":{"results":[{"Name":"BASKETSTATUS","Value":"5"}]}}'
    result = parse_download_end(body, PORTAL)
    assert result.error is True
    assert result.basket_status == 5


def test_download_end_marketplace():
    assert parse_download_end("OK, no problems", MARKETPLACE).error is False
    assert parse_download_end("an error occurred", MARKETPLACE).error is True


# ------------------------------------------------------------------------ allowlist

@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://download.sap.com/file.sar", True),
        ("https://cdn.edgekey.net/x", True),
        ("https://download.deploy.akamaitechnologies.net/x", True),
        ("https://evil.example.com/file.sar", False),
        ("https://sapevil.com/file.sar", False),  # suffix must include the dot
        ("https://host.wdf.sap.corp/file.sar", False),  # not in original allowlist
        ("not a url", False),
    ],
)
def test_download_url_allowlist(url, expected):
    assert is_valid_download_url(url) is expected
