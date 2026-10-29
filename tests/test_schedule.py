"""Schedule tests: API validation, firing, restart persistence, autostart."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from sapdlm.api.app import create_app
from sapdlm.api.service import Service
from sapdlm.config import Settings, save_settings
from sapdlm.credentials import CredentialStore
from tests.test_api import portal_handler


def make_client(tmp_path: Path, monkeypatch, autostart=False, pre_schedule=None) -> TestClient:
    data_dir = tmp_path / "data"
    download_dir = tmp_path / "store"
    monkeypatch.setenv("DLM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DLM_DOWNLOAD_DIR", str(download_dir))
    data_dir.mkdir(parents=True, exist_ok=True)
    transport = httpx.MockTransport(portal_handler)

    if autostart or pre_schedule:
        settings = Settings(
            sap_url="https://launchpad.test",
            username="jane",
            download_store=str(download_dir),
            autostart_download=autostart,
        )
        if pre_schedule:
            settings.schedule_at = pre_schedule
            settings.schedule_mode = "all"
        save_settings(data_dir, settings)
        CredentialStore(data_dir).set("sap_password", "s3cret")

    def service_factory(data_dir, download_dir, **kw):
        return Service(
            data_dir=data_dir,
            download_dir=download_dir,
            http_client=httpx.Client(transport=transport),
            component_client_factory=lambda: httpx.Client(transport=transport),
        )

    return TestClient(create_app(service_factory=service_factory))


def configure(client: TestClient) -> None:
    client.put("/api/config", json={"sap_url": "https://launchpad.test", "username": "jane"})
    client.put("/api/credentials", json={"sap_password": "s3cret"})


# ------------------------------------------------------------------- validation


def test_schedule_validation(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        # Not configured: setting a schedule is rejected
        past = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
        resp = client.post("/api/schedule", json={"schedule_at": past})
        assert resp.status_code == 409

        configure(client)
        # Past date -> original error text
        resp = client.post("/api/schedule", json={"schedule_at": past})
        assert resp.status_code == 400
        assert "lies before the current date and time" in resp.json()["detail"]

        # Future date -> armed
        future = (datetime.now().astimezone() + timedelta(hours=1)).isoformat()
        resp = client.post("/api/schedule", json={"schedule_at": future})
        assert resp.status_code == 200
        assert resp.json()["active"] is True

        # Empty string clears
        resp = client.post("/api/schedule", json={"schedule_at": ""})
        assert resp.json()["active"] is False
        assert resp.json()["schedule_at"] == ""

        # DELETE clears too
        client.post("/api/schedule", json={"schedule_at": future})
        assert client.delete("/api/schedule").status_code == 204
        assert client.get("/api/schedule").json()["active"] is False


# ---------------------------------------------------------------------- firing


def test_schedule_fires_download(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        configure(client)
        wait_task(client, client.post("/api/basket/refresh").json()["task_id"])

        soon = (datetime.now().astimezone() + timedelta(seconds=1)).isoformat()
        resp = client.post("/api/schedule", json={"schedule_at": soon})
        assert resp.json()["active"] is True

        deadline = time.time() + 15
        while time.time() < deadline:
            if client.get("/api/history").json()["entries"]:
                break
            time.sleep(0.1)
        entries = client.get("/api/history").json()["entries"]
        assert entries, "scheduled download did not run"
        assert entries[0]["dockey"] == "K1"
        # One-shot: cleared after firing
        assert client.get("/api/schedule").json()["active"] is False


def test_missed_schedule_cleared_on_startup(tmp_path, monkeypatch):
    past = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()
    with make_client(tmp_path, monkeypatch, pre_schedule=past) as client:
        state = client.get("/api/state").json()
        assert state["configured"] is True
        schedule = client.get("/api/schedule").json()
        assert schedule["active"] is False
        assert schedule["schedule_at"] == ""


# -------------------------------------------------------------------- autostart


def test_autostart_downloads_on_startup(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, autostart=True) as client:
        deadline = time.time() + 20
        while time.time() < deadline:
            if client.get("/api/history").json()["entries"]:
                break
            time.sleep(0.1)
        entries = client.get("/api/history").json()["entries"]
        assert entries, "autostart did not download the basket"
        assert (tmp_path / "store" / "comp.bin").exists()


def wait_task(client: TestClient, task_id: str, timeout=10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["state"] != "running":
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish")
