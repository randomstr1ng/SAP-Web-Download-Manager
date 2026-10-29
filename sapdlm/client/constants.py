"""Constants mirrored from the original com.sap.fcms.dlclient classes.

Mirrors com.sap.fcms.dlclient of the official client (see docs/PROTOCOL.md).
"""

from __future__ import annotations

from sapdlm import DLM_VERSION

__all__ = [
    "DLM_VERSION",
    "MAX_HISTORY_ENTRIES",
    "STATUS_COMPLETE",
    "STATUS_DOWNLOADED_BASKET_UNSET",
    "STATUS_IN_PROGRESS",
    "STATUS_NOT_STARTED",
    "STATUS_PARTIAL",
    "VALID_DOWNLOAD_HOST_SUFFIXES",
    "BasketStatus",
    "FileStatus",
    "GlobalStatus",
    "LocalStatus",
]


# BasketItem.GLOBALSTATUS_*
class GlobalStatus:
    OK = 0
    DELETED = 1  # "This object is no longer available."
    NOT_AUTHORIZED = 2  # "You are not authorized to download selected object."
    ERR_OTHER = 3  # item error text
    SYSTEM_ERROR = 5  # "System error occurs. Try again later."


# BasketItem.BASKETSTATUS_*
class BasketStatus:
    EMPTY = 0
    NEW = 1
    UPDATE = 2
    DL_STARTED = 3
    DL_FINISHED = 4
    ERROR = 5
    DELETED = 6


# LocalStatusItem.LOCALSTATUS_*
class LocalStatus:
    NOT_STARTED = 0
    PARTIAL = 1
    DOWNLOADED = 2  # downloaded, basket status not set
    COMPLETE = 3


# LocalStatusFile.FILEDOWNLOAD_*
class FileStatus:
    NOT_STARTED = 0
    RUNNING = 1
    COMPLETED = 2


# DownloadHandler.ProcessItemThread.isValid() host-suffix allowlist.
VALID_DOWNLOAD_HOST_SUFFIXES = (
    ".sap-ag.de",
    ".sap.com",
    ".akadns.net",
    ".akamai.net",
    ".akamaidrm.net",
    ".akamaiedge.net",
    ".deploy.akamaitechnologies.net",
    ".edgekey.net",
    ".edgesuite.net",
)

# DownloadHistory.MAX_ENTRIES
MAX_HISTORY_ENTRIES = 250

# Item status texts (MainFrame.getItemStatusText) - reused verbatim by the API.
STATUS_NOT_STARTED = "Download not started"
STATUS_DOWNLOADED_BASKET_UNSET = "Downloaded, basket status not set"
STATUS_COMPLETE = "Completely downloaded"
STATUS_PARTIAL = "Partially downloaded"
STATUS_IN_PROGRESS = "Download is in progress."
