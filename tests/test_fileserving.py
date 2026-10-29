"""Tests for serving completed downloads over the web UI (#8)."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from tests.portal_stub import COMP_BODY
from tests.test_api import configure, make_client, wait_task


def _downloaded_client(tmp_path, monkeypatch) -> TestClient:
    with make_client(tmp_path, monkeypatch=monkeypatch) as client:
        configure(client)
        wait_task(client, client.post("/api/basket/refresh").json()["task_id"])
        wait_task(client, client.post("/api/downloads/start",
                                     json={"keys": ["K1"]}).json()["task_id"])
        return client


def test_file_listing_after_download(tmp_path, monkeypatch):
    with _downloaded_client(tmp_path, monkeypatch) as client:
        listing = client.get("/api/objects/K1/files")
        assert listing.status_code == 200
        files = listing.json()["files"]
        assert [f["file_name"] for f in files] == ["comp.bin"]
        assert files[0]["size"] == len(COMP_BODY)


def test_file_served_full(tmp_path, monkeypatch):
    with _downloaded_client(tmp_path, monkeypatch) as client:
        resp = client.get("/api/objects/K1/files/comp.bin")
        assert resp.status_code == 200
        assert resp.content == COMP_BODY
        assert resp.headers["accept-ranges"] == "bytes"
        assert "attachment" in resp.headers["content-disposition"]
        assert "comp.bin" in resp.headers["content-disposition"]


def test_range_requests(tmp_path, monkeypatch):
    with _downloaded_client(tmp_path, monkeypatch) as client:
        # start-end
        resp = client.get("/api/objects/K1/files/comp.bin",
                          headers={"Range": "bytes=0-99"})
        assert resp.status_code == 206
        assert resp.content == COMP_BODY[:100]
        assert resp.headers["content-range"] == f"bytes 0-99/{len(COMP_BODY)}"
        # open-ended
        resp = client.get("/api/objects/K1/files/comp.bin",
                          headers={"Range": f"bytes={len(COMP_BODY) - 10}-"})
        assert resp.status_code == 206
        assert resp.content == COMP_BODY[-10:]
        # suffix
        resp = client.get("/api/objects/K1/files/comp.bin",
                          headers={"Range": "bytes=-10"})
        assert resp.status_code == 206
        assert resp.content == COMP_BODY[-10:]
        # unsatisfiable
        resp = client.get("/api/objects/K1/files/comp.bin",
                          headers={"Range": f"bytes={len(COMP_BODY) + 5}-"})
        assert resp.status_code == 416


def test_unknown_and_foreign_files_not_served(tmp_path, monkeypatch):
    with _downloaded_client(tmp_path, monkeypatch) as client:
        # unknown object
        assert client.get("/api/objects/NOPE/files/comp.bin").status_code == 404
        # file not part of this object
        assert client.get("/api/objects/K1/files/other.bin").status_code == 404
        # traversal attempt: never matches a component name, and any on-disk
        # lookup is confined to the store anyway
        resp = client.get("/api/objects/K1/files/..%2f..%2fsecret.bin")
        assert resp.status_code == 404


def test_incomplete_file_not_served(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch=monkeypatch) as client:
        configure(client)
        wait_task(client, client.post("/api/basket/refresh").json()["task_id"])
        # object known to the store, but nothing downloaded yet
        assert client.get("/api/objects/K1/files/comp.bin").status_code == 404
