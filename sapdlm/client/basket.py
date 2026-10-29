"""Basket / download-start data models (BasketItems, BasketItem, DownloadHandler).

Field semantics mirror the original classes (see docs/PROTOCOL.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sapdlm.client.constants import GlobalStatus, LocalStatus


class ResponseParseError(Exception):
    """Original: ResponseParseException - basket/object data could not be parsed
    or the portal reported ERRORCODE/ERRORINFO."""


@dataclass
class BasketItem:
    dockey: str
    doc_type: str = ""
    text: str = ""
    size_kb: int = 0
    basket_status: int = 0
    global_status: int = GlobalStatus.OK
    reg_date: str = ""  # YYYYMMDD
    reg_time: str = ""  # hhmmss
    error_text: str = ""
    download_complete: bool = False

    @property
    def size_text(self) -> str:
        if self.size_kb == 0:
            return "unknown size."
        return f"{self.size_kb:,} KB"

    @property
    def reg_datetime_key(self) -> str:
        return self.reg_date + self.reg_time

    def status_text(self, local_status: int | None, downloading: bool = False) -> str:
        """Mirror of MainFrame.getItemStatusText."""
        if self.download_complete:
            return "This object was completely downloaded."
        if downloading:
            return "Download is in progress."
        if self.global_status == GlobalStatus.DELETED:
            return "This object is no longer available."
        if self.global_status == GlobalStatus.NOT_AUTHORIZED:
            return "You are not authorized to download selected object."
        if self.global_status == GlobalStatus.ERR_OTHER:
            return self.error_text or "An error occurred."
        if self.global_status == GlobalStatus.SYSTEM_ERROR:
            return "System error occurs. Try again later."
        if local_status is None:
            return "Download not started"
        if local_status == LocalStatus.NOT_STARTED:
            return "Download not started"
        if local_status == LocalStatus.DOWNLOADED:
            return "Downloaded, basket status not set"
        if local_status == LocalStatus.COMPLETE:
            return "Completely downloaded"
        if local_status == LocalStatus.PARTIAL:
            return "Partially downloaded"
        return "Download not started"


@dataclass
class UpgradeNotice:
    """DLM_UPG_* fields: newer Download Manager available."""

    version: str | None = None
    exp_date: str | None = None
    info: str | None = None
    url: str | None = None


@dataclass
class BasketContent:
    """Parsed BASKET_CONTENT response (BasketItems)."""

    items: dict[str, BasketItem] = field(default_factory=dict)
    full_name: str = ""
    user_ip: str = ""
    servers: list[str] = field(default_factory=list)
    version: str = ""
    info_page: str = ""
    welcome_page: str = ""
    help_page: str = ""
    sap_port: int = 0
    upgrade: UpgradeNotice = field(default_factory=UpgradeNotice)
    error_code: str = ""
    error_info: str = ""


@dataclass
class ObjectVersion:
    """Parsed OBJECT_VERSION response (BasketItem.parseStatusInfo)."""

    global_status: int = GlobalStatus.OK
    reported_version: int = 0
    error_code: str = ""
    error_info: str = ""


@dataclass
class DownloadComponent:
    name: str  # DOWNLOAD<n> key
    url: str  # resource URL (absolute or relative)
    token: str = ""  # cookie value ("name=value" pair string, like the original)


@dataclass
class DownloadStart:
    """Parsed DOWNLOAD_START2 response."""

    dockey: str = ""
    components: list[DownloadComponent] = field(default_factory=list)
    description: list[str] = field(default_factory=list)
    description_conversion: str | None = None
    error_code: str = ""


@dataclass
class DownloadEnd:
    """Parsed DOWNLOAD_END response - the portal must confirm BASKETSTATUS=4."""

    basket_status: int = -1
    error: bool = False
