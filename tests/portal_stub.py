"""Shared stub portal for tests and the UI smoke mock server.

Serves one object (dockey K1) over the same OData wire format as the SAP
Support Portal; see docs/PROTOCOL.md.
"""

from __future__ import annotations

import httpx

COMP_BODY = b"B" * 100_000


def portal_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/odata/svt/swdcuisrv/DownloadContentSet":
        mode = httpx.QueryParams(request.url.query).get("_MODE")
        if mode == "BASKET_CONTENT":
            body = {"d": {"results": [
                {"Name": "USERNAME1", "Value": "Jane"},
                {"Name": "USERNAME2", "Value": "Doe"},
                {"Name": "OBJID",
                 "Value": "K1|SAR|NetWeaver kernel|100|1|20241129120000|0"},
            ]}}
        elif mode == "OBJECT_VERSION":
            body = {"d": {"results": [
                {"Name": "GLOBALSTATUS", "Value": "0"},
                {"Name": "INFOHDVERSION", "Value": "17"},
            ]}}
        elif mode == "DOWNLOAD_START2":
            body = {"d": {"results": [
                {"Name": "DOCKEY", "Value": "K1"},
                {"Name": "DOWNLOAD0_comp.bin", "Value": "https://download.sap.com/comp.bin"},
                {"Name": "TOKEN_SAPSSO", "Value": "tok"},
                {"Name": "DESCRIPTION", "Value": "Kernel 7.49"},
            ]}}
        elif mode == "DOWNLOAD_END":
            body = {"d": {"results": [{"Name": "BASKETSTATUS", "Value": "4"}]}}
        elif mode == "OBJDEL":
            body = {"d": {"results": []}}
        else:
            return httpx.Response(400)
        return httpx.Response(200, json=body)
    if path == "/comp.bin":
        range_header = request.headers.get("Range", "")
        if range_header:
            start = int(range_header.split("bytes=")[1].split("-")[0])
            body = COMP_BODY[start:]
            return httpx.Response(206, content=body, headers={
                "Content-Range": f"bytes {start}-{len(COMP_BODY)-1}/{len(COMP_BODY)}",
                "Content-Length": str(len(body)),
            })
        return httpx.Response(200, content=COMP_BODY,
                              headers={"Content-Length": str(len(COMP_BODY))})
    return httpx.Response(404)
