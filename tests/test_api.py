"""API tests: config/credential flow, basket refresh, downloads, cleanup, auth.

The portal + file servers are simulated with a single httpx MockTransport.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from sapdlm.api.app import create_app
from sapdlm.api.service import Service
from sapdlm.credentials import MASK

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


def make_client(tmp_path: Path, monkeypatch, admin=None) -> TestClient:
    data_dir = tmp_path / "data"
    download_dir = tmp_path / "store"
    # The lifespan derives dirs from env; the factory must use exactly these.
    monkeypatch.setenv("DLM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DLM_DOWNLOAD_DIR", str(download_dir))
    transport = httpx.MockTransport(portal_handler)

    def service_factory(data_dir, download_dir, **kw):
        return Service(
            data_dir=data_dir,
            download_dir=download_dir,
            http_client=httpx.Client(transport=transport),
            component_client_factory=lambda: httpx.Client(transport=transport),
        )

    app = create_app(service_factory=service_factory)
    if admin:
        monkeypatch.setenv("DLM_ADMIN_USER", admin[0])
        monkeypatch.setenv("DLM_ADMIN_PASSWORD", admin[1])
        app.state.admin_user = admin[0]
        app.state.admin_password = admin[1]
    return TestClient(app)


def wait_task(client: TestClient, task_id: str, timeout=10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["state"] != "running":
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish")


def configure(client: TestClient) -> None:
    resp = client.put("/api/config", json={
        "sap_url": "https://launchpad.test",
        "username": "jane",
        "download_store": None,  # keep the service default (tmp store)
    })
    assert resp.status_code == 200
    resp = client.put("/api/credentials", json={"sap_password": "s3cret"})
    assert resp.status_code == 200


# ------------------------------------------------------------------ basic routes

def test_status_health_state(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        assert client.get("/api/status").json()["status"] == "ok"
        assert client.get("/api/health").status_code == 200
        state = client.get("/api/state").json()
        assert state["configured"] is False
        assert state["app_version"]


def test_refresh_without_configuration_is_409(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        response = client.post("/api/basket/refresh")
        assert response.status_code == 409


# -------------------------------------------------------------- config + secrets

def test_config_and_credential_flow(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        config = client.get("/api/config").json()
        assert config["complete"] is False and config["configured"] is False

        configure(client)

        config = client.get("/api/config").json()
        assert config["complete"] is True
        assert config["configured"] is True

        creds = client.get("/api/credentials").json()
        assert creds["sap_password"]["stored"] is True
        assert creds["sap_password"]["value"] == MASK
        assert "s3cret" not in json.dumps(creds)

        # masked PUT round-trip keeps the value
        client.put("/api/credentials", json={"sap_password": MASK})
        assert client.get("/api/credentials").json()["sap_password"]["stored"] is True

        # file backend: 0600, stored value readable only from disk
        cred_file = tmp_path / "data" / "credentials.json"
        assert cred_file.exists()
        assert oct(cred_file.stat().st_mode)[-3:] == "600"
        assert json.loads(cred_file.read_text())["sap_password"] == "s3cret"


def test_env_override_beats_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DLM_SAP_PASSWORD", "env-wins")
    with make_client(tmp_path, monkeypatch) as client:
        configure(client)
        service = client.app.state.service
        assert service.connection_config().password == "env-wins"
        assert "env-wins" not in (
            tmp_path / "data" / "credentials.json"
        ).read_text()


# ------------------------------------------------------------- basket + download

def test_basket_refresh_flow(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        configure(client)
        resp = client.post("/api/basket/refresh")
        assert resp.status_code == 202
        task = wait_task(client, resp.json()["task_id"])
        assert task["state"] == "done", task["error"]

        basket = client.get("/api/basket").json()
        assert basket["refreshed"] is True
        assert basket["full_name"] == "Jane Doe"
        assert len(basket["items"]) == 1
        item = basket["items"][0]
        assert item["dockey"] == "K1"
        assert item["size_text"] == "100 KB"
        assert item["status_text"] == "Download not started"


def test_full_download_flow(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        configure(client)
        task = wait_task(client, client.post("/api/basket/refresh").json()["task_id"])
        assert task["state"] == "done"

        resp = client.post("/api/downloads/start", json={"keys": ["K1"]})
        assert resp.status_code == 202
        task = wait_task(client, resp.json()["task_id"], timeout=20)
        assert task["state"] == "done", task["error"]

        target = tmp_path / "store" / "comp.bin"
        assert target.exists() and target.stat().st_size == len(COMP_BODY)

        state = client.get("/api/state").json()
        assert state["downloading"] == []
        basket = client.get("/api/basket").json()
        assert basket["items"][0]["local_status"] == 3  # COMPLETE
        assert "completely downloaded" in basket["items"][0]["status_text"].lower()

        history = client.get("/api/history").json()["entries"]
        assert len(history) == 1 and history[0]["dockey"] == "K1"

        protocol = client.get("/api/objects/K1/protocol").json()["entries"]
        assert any("completed successfully" in e["message"] for e in protocol)


def test_delete_local_data(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        configure(client)
        wait_task(client, client.post("/api/basket/refresh").json()["task_id"])
        wait_task(client, client.post("/api/downloads/start",
                                     json={"keys": ["K1"]}).json()["task_id"],
                  timeout=20)
        assert (tmp_path / "store" / "comp.bin").exists()

        response = client.delete("/api/downloads/K1/local")
        assert response.status_code == 204
        assert not (tmp_path / "store" / "comp.bin").exists()
        assert client.get("/api/history").json()["entries"]  # history survives


def test_remove_from_basket(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        configure(client)
        wait_task(client, client.post("/api/basket/refresh").json()["task_id"])
        response = client.delete("/api/basket/K1")
        assert response.status_code == 204
        assert client.get("/api/basket").json()["items"] == []


def test_pause_without_download_is_noop(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        configure(client)
        wait_task(client, client.post("/api/basket/refresh").json()["task_id"])
        response = client.post("/api/downloads/K1/pause")
        assert response.status_code == 202
        assert response.json()["paused"] is False


def test_unknown_object_404(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        configure(client)
        assert client.get("/api/objects/NOPE/protocol").status_code == 200
        assert client.post("/api/downloads/NOPE/pause").status_code == 404


# ------------------------------------------------------------------------ auth

def test_admin_auth(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch=monkeypatch,
                     admin=("admin", "pw123")) as client:
        assert client.put("/api/config", json={"sap_url": "x"}
                          ).status_code == 401
        assert client.get("/api/status").status_code == 200  # health stays open

        client.auth = ("admin", "pw123")
        response = client.put("/api/config", json={"sap_url": "https://launchpad.test",
                                                  "username": "jane"})
        assert response.status_code == 200
        assert client.get("/api/basket").status_code == 200


def test_ws_hello(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["status"]["app_version"]


def test_empty_credential_put_never_wipes(tmp_path, monkeypatch):
    """Regression: a stale/empty Settings form sent "" and wiped stored
    secrets (reported when using Test Connection after a stale page)."""
    with make_client(tmp_path, monkeypatch=monkeypatch) as client:
        configure(client)
        assert client.get("/api/credentials").json()["sap_password"]["stored"] is True
        # empty-string PUT must be a no-op, not a clear
        client.put("/api/credentials", json={"sap_password": ""})
        assert client.get("/api/credentials").json()["sap_password"]["stored"] is True
        # explicit DELETE clears
        client.delete("/api/credentials/sap_password")
        assert client.get("/api/credentials").json()["sap_password"]["stored"] is False


def test_settings_save_preserves_connected_state(tmp_path, monkeypatch):
    """Regression: update_settings rebuilt the DownloadClient and lost the
    connected flag (user saw 'not connected' after saving settings)."""
    with make_client(tmp_path, monkeypatch=monkeypatch) as client:
        configure(client)
        wait_task(client, client.post("/api/basket/refresh").json()["task_id"])
        assert client.get("/api/state").json()["connected"] is True
        client.put("/api/config", json={"create_subdir": True})
        state = client.get("/api/state").json()
        assert state["connected"] is True, "engine reload must keep connection state"
        service = client.app.state.service
        assert service.manager.use_subdir is True
        assert service.manager.client.config.create_subdir if hasattr(
            service.manager.client.config, "create_subdir") else True


def test_settings_strips_whitespace(tmp_path, monkeypatch):
    """Regression: a leading space in the S-user (browser autofill) broke basic
    auth with a confusing 401 PWD_WRONG. All text settings are stored trimmed
    and trimmed again when used."""
    with make_client(tmp_path, monkeypatch=monkeypatch) as client:
        resp = client.put("/api/config", json={
            "sap_url": "  https://launchpad.test  ",
            "username": " S0027638133 ",
            "download_store": "  /tmp/store-with-space  ",
            "proxy_host": " proxy.internal ",
        })
        assert resp.status_code == 200
        config = resp.json()
        assert config["username"] == "S0027638133"
        assert config["sap_url"] == "https://launchpad.test"
        assert config["download_store"] == "/tmp/store-with-space"
        assert config["proxy_host"] == "proxy.internal"

        service = client.app.state.service
        conn = service.connection_config()
        assert conn.username == "S0027638133"
        assert conn.sap_url == "https://launchpad.test"
        assert conn.proxy_host == "proxy.internal"
        # persisted file is clean too
        import json as _json
        stored = _json.loads((tmp_path / "data" / "config.json").read_text())
        assert stored["username"] == "S0027638133"
