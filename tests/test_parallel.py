"""Tests for configurable parallel object downloads (#7)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from sapdlm.api.app import create_app
from sapdlm.api.service import Service

BODY = {f"K{i}.bin": b"X" * 10_000 for i in (1, 2)}


class Portal:
    """Stub portal serving two objects whose component downloads block,
    so real overlap is observable."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = httpx.QueryParams(request.url.query)
        path = request.url.path
        if path.endswith("/DownloadContentSet"):
            mode = params.get("_MODE")
            if mode == "BASKET_CONTENT":
                body = {
                    "d": {
                        "results": [
                            {
                                "Name": "OBJID",
                                "Value": f"K{i}|SAR|Object {i}|100|1|20241129120000|0",
                            }
                            for i in (1, 2)
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
            with self.lock:
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                time.sleep(0.25)  # give a parallel object time to join in
                return httpx.Response(200, content=BODY[path.rsplit("/", 1)[1]])
            finally:
                with self.lock:
                    self.inflight -= 1
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


def configure(client: TestClient, concurrent: int) -> None:
    resp = client.put(
        "/api/config",
        json={
            "sap_url": "https://launchpad.test",
            "username": "jane",
            "download_store": None,
            "max_concurrent_objects": concurrent,
        },
    )
    assert resp.status_code == 200
    assert client.put("/api/credentials", json={"sap_password": "s3cret"}).status_code == 200


def wait_task(client: TestClient, task_id: str, timeout=15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["state"] != "running":
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish")


def _downloaded(client: TestClient, dockey: str) -> dict:
    return client.get(f"/api/objects/{dockey}").json()


def test_parallel_objects_download_concurrently(tmp_path, monkeypatch):
    portal = Portal()
    with make_client(tmp_path, monkeypatch, portal) as client:
        configure(client, concurrent=2)
        wait_task(client, client.post("/api/basket/refresh").json()["task_id"])
        resp = client.post("/api/downloads/start", json={"mode": "all"})
        wait_task(client, resp.json()["task_id"])

        assert portal.max_inflight == 2, (
            "component downloads did not overlap with max_concurrent_objects=2"
        )
        for dockey in ("K1", "K2"):
            detail = _downloaded(client, dockey)
            assert detail["local_status"] in (2, 3)  # completed


def test_default_remains_sequential(tmp_path, monkeypatch):
    portal = Portal()
    with make_client(tmp_path, monkeypatch, portal) as client:
        configure(client, concurrent=1)
        wait_task(client, client.post("/api/basket/refresh").json()["task_id"])
        resp = client.post("/api/downloads/start", json={"mode": "all"})
        wait_task(client, resp.json()["task_id"])

        assert portal.max_inflight == 1
        for dockey in ("K1", "K2"):
            detail = _downloaded(client, dockey)
            assert detail["local_status"] in (2, 3)


def test_setting_clamped_to_minimum_one(tmp_path, monkeypatch):
    portal = Portal()
    with make_client(tmp_path, monkeypatch, portal) as client:
        resp = client.put(
            "/api/config",
            json={
                "sap_url": "https://launchpad.test",
                "username": "jane",
                "download_store": None,
                "max_concurrent_objects": 0,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["max_concurrent_objects"] == 1
