"""Endpoint styles and request-path builders (parity with DownloadClient/BasketItem/DownloadHandler).

Two endpoint families exist (see docs/PROTOCOL.md), selected by the original with
``sHost.contains("service")`` - Service Marketplace (legacy key=value) vs Support
Portal (OData JSON). We model this explicitly.
"""

from __future__ import annotations

import platform
import sys
import urllib.parse
from dataclasses import dataclass
from enum import Enum

from sapdlm import DLM_VERSION


class EndpointStyle(Enum):
    MARKETPLACE = "marketplace"
    PORTAL = "portal"


def detect_style(sap_url: str) -> EndpointStyle:
    """Mirror of ``sHost.contains("service")`` (URL string check)."""
    return EndpointStyle.MARKETPLACE if "service" in sap_url else EndpointStyle.PORTAL


def _client_info_params() -> dict[str, str]:
    """J_VERSION/OS_* telemetry params sent with BASKET_CONTENT (DownloadClient.getUserBasketUri).

    The original reports the local Java/OS environment; we report ours truthfully.
    """
    return {
        "J_VERSION": ".".join(map(str, sys.version_info[:3])),
        "J_VM_VERSION": sys.implementation.name,
        "J_VENDOR": sys.implementation.name,
        "J_VM_NAME": f"{platform.python_implementation()} ({sys.implementation.name})",
        "OS_NAME": platform.system(),
        "OS_ARCH": platform.machine(),
        "OS_VERSION": platform.release(),
        "_VERSION": DLM_VERSION,
    }


def _q(params: dict[str, str]) -> str:
    return "&".join(f"{k}={urllib.parse.quote_plus(v)}" for k, v in params.items())


@dataclass(frozen=True)
class EndpointBuilder:
    style: EndpointStyle

    def basket_content(self) -> str:
        params = {"_MODE": "BASKET_CONTENT", **_client_info_params()}
        if self.style is EndpointStyle.MARKETPLACE:
            return "/~form/download_basket?" + _q(params)
        return "/odata/svt/swdcuisrv/DownloadContentSet?" + _q(params) + "&$format=json&"

    def _op(self, mode: str, dockey: str) -> str:
        params = {"_MODE": mode, "OBJID": dockey, "_VERSION": DLM_VERSION}
        if self.style is EndpointStyle.MARKETPLACE:
            return "/~form/download_basket?" + _q(params) + "&"
        return "/odata/svt/swdcuisrv/DownloadContentSet?" + _q(params) + "&$format=json&"

    def object_version(self, dockey: str) -> str:
        return self._op("OBJECT_VERSION", dockey)

    def download_start(self, dockey: str) -> str:
        return self._op("DOWNLOAD_START2", dockey)

    def download_end(self, dockey: str) -> str:
        return self._op("DOWNLOAD_END", dockey)

    def object_delete(self, dockey: str) -> str:
        return self._op("OBJDEL", dockey)
