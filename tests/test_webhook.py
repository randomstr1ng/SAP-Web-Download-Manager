"""Tests for webhook notifications on download completion/failure (#9)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from sapdlm.api.app import create_app
from sapdlm.api.service import Service
from tests.test_api import configure, portal_handler, wait_task


def make_client(
    tmp_path: Path, monkeypatch, posts: list[dict], hook_url: str = "https://hook.test/notify"
) -> TestClient:
    """A client whose service delivers webhooks into ``posts``."""
    data_dir = Path(tmp_path) / "data"
    download_dir = Path(tmp_path) / "store"
    monkeypatch.setenv("DLM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DLM_DOWNLOAD_DIR", str(download_dir))
    transport = httpx.MockTransport(portal_handler)

    def hook_handler(request: httpx.Request) -> httpx.Response:
        posts.append(
            {
                "url": str(request.url),
                "body": request.read(),
                "headers": dict(request.headers),
            }
        )
        return httpx.Response(200)

    def service_factory(data_dir, download_dir, **kw):
        return Service(
            data_dir=data_dir,
            download_dir=download_dir,
            http_client=httpx.Client(transport=transport),
            component_client_factory=lambda: httpx.Client(transport=transport),
            webhook_client=httpx.Client(transport=httpx.MockTransport(hook_handler)),
        )

    client = TestClient(create_app(service_factory=service_factory))
    with client:
        configure(client)
        assert client.put("/api/config", json={"webhook_url": hook_url}).status_code == 200
        assert client.put("/api/credentials", json={"webhook_secret": "shh"}).status_code == 200
    return client


def test_webhook_on_completed_download(tmp_path, monkeypatch):
    posts: list[dict] = []
    with make_client(tmp_path, monkeypatch, posts) as client:
        wait_task(client, client.post("/api/basket/refresh").json()["task_id"])
        wait_task(
            client, client.post("/api/downloads/start", json={"keys": ["K1"]}).json()["task_id"]
        )

    assert len(posts) == 1
    payload = json.loads(posts[0]["body"])
    assert posts[0]["url"] == "https://hook.test/notify"
    assert payload["event"] == "completed"
    assert payload["dockey"] == "K1"
    assert payload["text"] == "NetWeaver kernel"
    assert payload["timestamp"]
    assert posts[0]["headers"].get("x-webhook-secret") == "shh"
    assert posts[0]["headers"].get("content-type") == "application/json"


def test_webhook_on_failed_download(tmp_path, monkeypatch):
    posts: list[dict] = []
    with make_client(tmp_path, monkeypatch, posts) as client:
        # No basket refresh -> the object is unknown to the engine -> failure.
        resp = client.post("/api/downloads/start", json={"keys": ["MISSING"]})
        wait_task(client, resp.json()["task_id"])

    assert len(posts) == 1
    payload = json.loads(posts[0]["body"])
    assert payload["event"] == "failed"
    assert payload["dockey"] == "MISSING"


def test_no_webhook_without_url(tmp_path, monkeypatch):
    posts: list[dict] = []
    with make_client(tmp_path, monkeypatch, posts, hook_url="") as client:
        wait_task(client, client.post("/api/basket/refresh").json()["task_id"])
        wait_task(
            client, client.post("/api/downloads/start", json={"keys": ["K1"]}).json()["task_id"]
        )
    assert posts == []


def test_webhook_secret_masked_over_api(tmp_path, monkeypatch):
    posts: list[dict] = []
    with make_client(tmp_path, monkeypatch, posts) as client:
        creds = client.get("/api/credentials").json()
    assert creds["webhook_secret"]["stored"] is True
    assert creds["webhook_secret"]["value"] == "******"
