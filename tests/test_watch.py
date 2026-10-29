"""Tests for watch mode: periodic refresh + auto-download of new items (#9)."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from sapdlm.api.app import create_app
from sapdlm.api.service import Service


class Portal:
    """Stub portal whose basket content can grow during the test."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = httpx.QueryParams(request.url.query)
        path = request.url.path
        if path.endswith("/DownloadContentSet"):
            mode = params.get("_MODE")
            if mode == "BASKET_CONTENT":
                body = {
                    "d": {
                        "results": [
                            {"Name": "OBJID", "Value": f"{k}|SAR|Object {k}|100|1|20241129120000|0"}
                            for k in self.items
                        ]
                    }
                }
            elif mode == "OBJECT_VERSION":
                body = {
                    "d": {
                        "results": [
                            {"Name": "GLOBALSTATUS", "Value": "0"},
                            {"Name": "INFOHDVERSION", "Value": "17"},
                        ]
                    }
                }
            elif mode == "DOWNLOAD_START2":
                dockey = params.get("OBJID")
                body = {
                    "d": {
                        "results": [
                            {"Name": "DOCKEY", "Value": dockey},
                            {
                                "Name": f"DOWNLOAD0_{dockey}.bin",
                                "Value": f"https://download.sap.com/{dockey}.bin",
                            },
                            {"Name": "TOKEN_SAPSSO", "Value": "tok"},
                            {"Name": "DESCRIPTION", "Value": f"Object {dockey}"},
                        ]
                    }
                }
            elif mode == "DOWNLOAD_END":
                body = {"d": {"results": [{"Name": "BASKETSTATUS", "Value": "4"}]}}
            elif mode == "OBJDEL":
                body = {"d": {"results": []}}
            else:
                return httpx.Response(400)
            return httpx.Response(200, json=body)
        if path.endswith(".bin"):
            return httpx.Response(200, content=b"X" * 1000)
        return httpx.Response(404)


def make_client(tmp_path: Path, monkeypatch, portal: Portal) -> TestClient:
    data_dir = tmp_path / "data"
    download_dir = tmp_path / "store"
    monkeypatch.setenv("DLM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DLM_DOWNLOAD_DIR", str(download_dir))
    transport = httpx.MockTransport(portal.handler)

    def service_factory(data_dir, download_dir, **kw):
        return Service(
            data_dir=data_dir,
            download_dir=download_dir,
            http_client=httpx.Client(transport=transport),
            component_client_factory=lambda: httpx.Client(transport=transport),
        )

    return TestClient(create_app(service_factory=service_factory))


def configure(client: TestClient, **extra) -> None:
    resp = client.put(
        "/api/config",
        json={
            "sap_url": "https://launchpad.test",
            "username": "jane",
            "download_store": None,
            **extra,
        },
    )
    assert resp.status_code == 200
    assert client.put("/api/credentials", json={"sap_password": "s3cret"}).status_code == 200


def _status(client: TestClient, dockey: str) -> int | None:
    resp = client.get(f"/api/objects/{dockey}")
    if resp.status_code != 200:
        return None
    return resp.json()["local_status"]


def _wait_completed(client: TestClient, dockey: str, timeout=10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _status(client, dockey) in (2, 3):  # completed
            return True
        time.sleep(0.1)
    return False


def test_watch_downloads_new_items_only(tmp_path, monkeypatch):
    portal = Portal()
    portal.items = ["K1"]
    with make_client(tmp_path, monkeypatch, portal) as client:
        # fast cycle for the test (the loop reads this before each sleep)
        client.app.state.service.watcher._interval_seconds = lambda: 0.2
        configure(client, watch_mode=True, watch_interval=15)

        assert client.get("/api/state").json()["watching"] is True
        # let the first refresh run: K1 seeds the baseline, is NOT downloaded
        time.sleep(1.0)
        assert _status(client, "K1") in (None, 0)

        portal.items = ["K1", "K2"]  # a new item appears on the portal
        assert _wait_completed(client, "K2")
        assert _status(client, "K1") in (None, 0)


def test_watch_off_downloads_nothing(tmp_path, monkeypatch):
    portal = Portal()
    portal.items = ["K1"]
    with make_client(tmp_path, monkeypatch, portal) as client:
        configure(client)  # watch mode stays off
        assert client.get("/api/state").json()["watching"] is False
        portal.items = ["K1", "K2"]
        time.sleep(1.0)
        assert _status(client, "K2") in (None, 0)


def test_watch_toggle_stops_the_loop(tmp_path, monkeypatch):
    portal = Portal()
    portal.items = ["K1"]
    with make_client(tmp_path, monkeypatch, portal) as client:
        client.app.state.service.watcher._interval_seconds = lambda: 0.2
        configure(client, watch_mode=True)
        assert client.get("/api/state").json()["watching"] is True
        configure(client, watch_mode=False)
        assert client.get("/api/state").json()["watching"] is False
        portal.items = ["K1", "K2"]
        time.sleep(1.0)
        assert _status(client, "K2") in (None, 0)
