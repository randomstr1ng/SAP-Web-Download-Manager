"""Endpoint-style detection and request-path builders."""

from sapdlm import DLM_VERSION
from sapdlm.client.endpoints import EndpointStyle, detect_style


def test_style_detection():
    assert detect_style("https://service.sap.com/swdc") is EndpointStyle.MARKETPLACE
    assert detect_style("https://launchpad.support.sap.com") is EndpointStyle.PORTAL


def test_basket_content_path_portal():
    from sapdlm.client.endpoints import EndpointBuilder

    path = EndpointBuilder(EndpointStyle.PORTAL).basket_content()
    assert path.startswith("/odata/svt/swdcuisrv/DownloadContentSet?")
    assert "_MODE=BASKET_CONTENT" in path
    assert "$format=json" in path
    assert f"_VERSION={DLM_VERSION}" in path
    assert "OS_NAME=" in path and "J_VERSION=" in path


def test_basket_content_path_marketplace():
    from sapdlm.client.endpoints import EndpointBuilder

    path = EndpointBuilder(EndpointStyle.MARKETPLACE).basket_content()
    assert path.startswith("/~form/download_basket?")
    assert "_MODE=BASKET_CONTENT" in path
    assert "$format=json" not in path


def test_operations_carry_objid_and_version():
    from sapdlm.client.endpoints import EndpointBuilder

    b = EndpointBuilder(EndpointStyle.PORTAL)
    for path, mode in (
        (b.object_version("OBJ1"), "OBJECT_VERSION"),
        (b.download_start("OBJ1"), "DOWNLOAD_START2"),
        (b.download_end("OBJ1"), "DOWNLOAD_END"),
        (b.object_delete("OBJ1"), "OBJDEL"),
    ):
        assert f"_MODE={mode}" in path
        assert "OBJID=OBJ1" in path
        assert f"_VERSION={DLM_VERSION}" in path
