"""Response parsers for both endpoint styles (BasketItems / BasketItem / DownloadHandler parity).

- Marketplace style: plain ``KEY=value`` lines.
- Portal style: OData JSON - a document of ``{"Name": ..., "Value": ...}`` pairs,
  collected in document order exactly like the original streaming JsonParser walk.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from urllib.parse import urlparse

from sapdlm.client.basket import (
    BasketContent,
    BasketItem,
    DownloadComponent,
    DownloadEnd,
    DownloadStart,
    ObjectVersion,
    ResponseParseError,
    UpgradeNotice,
)
from sapdlm.client.constants import VALID_DOWNLOAD_HOST_SUFFIXES
from sapdlm.client.endpoints import EndpointStyle

_DESC_CONV_GUID = "{1F3AAA90-4782-4632-8C5E-E92F7A7751E4}"


def is_valid_download_url(url: str) -> bool:
    """DownloadHandler.ProcessItemThread.isValid - host-suffix allowlist."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return any(host.lower().endswith(s) for s in VALID_DOWNLOAD_HOST_SUFFIXES)


# --------------------------------------------------------------------------- helpers


def _name_value_pairs(node: object) -> Iterator[tuple[str, str]]:
    """Yield ("Name", "Value") pairs from a JSON document in document order."""
    if isinstance(node, dict):
        if "Name" in node and "Value" in node and len(node) <= 4:
            yield str(node["Name"]), str(node["Value"])
            return
        for value in node.values():
            yield from _name_value_pairs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _name_value_pairs(item)


def _key_value_lines(text: str) -> Iterator[tuple[str, str]]:
    """Yield (KEY, value) pairs from ``KEY=value`` lines (first ``=`` splits)."""
    for line in text.splitlines():
        pos = line.find("=")
        if pos == -1:
            raise ResponseParseError(f"Invalid line in basket content data: {line}")
        yield line[:pos].upper(), line[pos + 1 :]


# --------------------------------------------------------------------- basket content


def parse_basket_content(text: str, style: EndpointStyle) -> BasketContent:
    if style is EndpointStyle.MARKETPLACE:
        return _basket_from_marketplace(text)
    return _basket_from_portal(text)


def _require_item(item: BasketItem | None) -> BasketItem:
    if item is None:
        raise ResponseParseError(
            "Error in basket content stream: Info Object attribute "
            "specification without Info Object specification."
        )
    return item


def _basket_from_marketplace(text: str) -> BasketContent:
    content = BasketContent()
    first_name = last_name = ""
    item: BasketItem | None = None

    for key, value in _key_value_lines(text):
        if key == "VERSION":
            content.version = value
        elif key == "HEFLPPAGE":  # (sic) original constant ITEM_HELPPAGE
            content.help_page = value
        elif key == "INFOPAGE":
            content.info_page = value
        elif key == "SAPPORT":
            content.sap_port = int(value.strip())
        elif key == "!USER_IP":
            content.user_ip = value
        elif key == "USERNAME1":
            first_name = value
        elif key == "USERNAME2":
            last_name = value
        elif key == "WELCOMEPAGE":
            content.welcome_page = value
        elif key == "OBJCNT":
            int(value.strip())
        elif key == "SERVER":
            content.servers.append(value.strip())
        elif key == "REGSTAMP" or key == "USERID":
            pass  # recognized-but-ignored by the original (traced as warning)
        elif key == "REGDATE":
            _require_item(item).reg_date = value
        elif key == "REGTIME":
            _require_item(item).reg_time = value
        elif key == "DOCKEY":
            item = BasketItem(dockey=value)
            content.items[value] = item
        elif key == "DOCTYPE":
            _require_item(item).doc_type = value
        elif key == "GLOBALSTATUS":
            _require_item(item).global_status = int(value.strip())
        elif key == "BASKETSTATUS":
            _require_item(item).basket_status = int(value.strip())
        elif key == "SIZE":
            try:
                _require_item(item).size_kb = int(value.strip())
            except ValueError:
                _require_item(item).size_kb = 0
        elif key == "TEXT":
            _require_item(item).text = value
        elif key == "ERRORCODE":
            content.error_code = value
        elif key == "ERRORINFO":
            content.error_info = value
        elif key == "DLM_UPG_VERSION":
            content.upgrade.version = value
        elif key == "DLM_UPG_EXPDATE":
            content.upgrade.exp_date = value
        elif key == "DLM_UPG_INFO":
            content.upgrade.info = value
        elif key == "DLM_UPG_URL":
            content.upgrade.url = value
        else:
            # Unknown tokens are traced and skipped by the original.
            continue

    if content.error_code:
        raise ResponseParseError(
            f"SAP Support Portal System error: [{content.error_code}] {content.error_info}"
        )
    content.full_name = f"{first_name} {last_name}".strip() if first_name else last_name
    return content


def _basket_from_portal(text: str) -> BasketContent:
    content = BasketContent()
    first_name = last_name = ""

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"Failed to read download basket data: {exc}") from exc

    for key, value in _name_value_pairs(document):
        if key == "USERNAME1":
            first_name = value
        elif key == "USERNAME2":
            last_name = value
        elif key == "DLM_UPG_URL":
            content.upgrade.url = value
        elif key == "DLM_UPG_INFO":
            content.upgrade.info = value
        elif key == "DLM_UPG_VERSION":
            content.upgrade.version = value
        elif key == "DLM_UPG_EXPDATE":
            content.upgrade.exp_date = value
        elif key == "OBJCNT":
            int(value.strip())
        elif key == "ERRORCODE":
            content.error_code = value
        elif key == "ERRORINFO":
            content.error_info = value
        elif key == "USERID":
            continue
        else:
            # Item attribute line: DOCKEY|DOCTYPE|TEXT|SIZE_KB|BASKETSTATUS|YYYYMMDDhhmmss|GLOBALSTATUS
            attrs = value.split("|")
            if len(attrs) < 7:
                raise ResponseParseError(f"Invalid item attributes in basket content: {value}")
            item = BasketItem(dockey=attrs[0])
            item.doc_type = attrs[1]
            item.text = attrs[2]
            try:
                item.size_kb = int(attrs[3])
            except ValueError:
                item.size_kb = 0
            item.basket_status = int(attrs[4])
            try:
                item.reg_time = attrs[5][8:]
            except IndexError:
                pass
            try:
                item.reg_date = attrs[5][:8]
            except IndexError:
                pass
            item.global_status = int(attrs[6])
            content.items[item.dockey] = item

    if content.error_code:
        raise ResponseParseError(
            f"SAP Support Portal System error: [{content.error_code}] {content.error_info}"
        )
    content.full_name = f"{first_name} {last_name}".strip() if first_name else last_name
    return content


# ----------------------------------------------------------------------- object version


def parse_object_version(text: str, style: EndpointStyle) -> ObjectVersion:
    if style is EndpointStyle.MARKETPLACE:
        info = ObjectVersion()
        for key, value in _key_value_lines(text):
            if key == "GLOBALSTATUS":
                info.global_status = int(value.strip())
            elif key == "INFOHDVERSION":
                info.reported_version = int(value.strip())
            elif key == "ERRORCODE":
                info.error_code = value
            elif key == "ERRORINFO":
                info.error_info = value
        if info.error_code:
            raise ResponseParseError(
                f"The SAP Support Portal reported an error: [{info.error_code}] {info.error_info}"
            )
        return info

    info = ObjectVersion()
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"Error reading info object data: {exc}") from exc

    for key, value in _name_value_pairs(document):
        if key == "GLOBALSTATUS":
            info.global_status = int(value.strip())
        elif key == "INFOHDVERSION":
            info.reported_version = int(value.strip())
        elif key == "ERRORCODE":
            info.error_code = value
            info.global_status = 2 if value == "NOT_AUTHORIZED" else 3
        elif key == "ERRORINFO":
            info.error_info = value

    if info.error_code:
        raise ResponseParseError(
            f"The SAP Support Portal reported an error: [{info.error_code}] {info.error_info}"
        )
    return info


# --------------------------------------------------------------------- download start


def parse_download_start(text: str, style: EndpointStyle) -> DownloadStart:
    start = DownloadStart()
    if style is EndpointStyle.MARKETPLACE:
        for key, value in _key_value_lines(text):
            _consume_download_start_pair(start, key, value)
    else:
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ResponseParseError(f"Error reading download start data: {exc}") from exc
        for key, value in _name_value_pairs(document):
            _consume_download_start_pair(start, key, value)
    return start


def _consume_download_start_pair(start: DownloadStart, key: str, value: str) -> None:
    if key in ("DOCKEY", "OBJID"):  # portal uses DOCKEY, marketplace OBJID
        start.dockey = value.strip()
    elif key.startswith("DOWNLOAD"):
        start.components.append(DownloadComponent(name=key, url=value))
    elif key.startswith("TOKEN"):
        # TOKEN_<rest> => "<rest>=<value>" attached to the most recent component.
        pos = key.find("_")
        token = f"{key[pos + 1 :]}={value}" if pos != -1 else f"{key}={value}"
        if start.components:
            start.components[-1].token = token
    elif key == "DESCRIPTION":
        start.description.append(value)
    elif key == "DESCRIPTION_CONVERSION":
        start.description.insert(0, f"{_DESC_CONV_GUID}={value.strip()}")
        start.description_conversion = value.strip()


# ----------------------------------------------------------------------- download end


def parse_download_end(text: str, style: EndpointStyle) -> DownloadEnd:
    end = DownloadEnd(error=True)
    if style is EndpointStyle.MARKETPLACE:
        # Original scans the whole body for the substring "error" (case-insensitive).
        end.error = "error" in text.lower()
        return end

    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return end
    for key, value in _name_value_pairs(document):
        if key == "BASKETSTATUS":
            end.basket_status = int(value)
            if end.basket_status == 4:
                end.error = False
    return end


def parse_upgrade_notice(content: BasketContent) -> UpgradeNotice | None:
    if content.upgrade.version or content.upgrade.url:
        return content.upgrade
    return None
