"""End-to-end regression test: backend download -> WebSocket progress events.

A throttled local HTTP server stands in for the SAP file server so the
component download takes a few seconds and progress events are emitted.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from sapdlm.api.app import create_app
from sapdlm.api.service import Service
from sapdlm.client.basket import DownloadComponent, DownloadStart
from sapdlm.client.connection import ConnectionConfig
from sapdlm.client.download_client import DownloadClient
from sapdlm.config import save_settings, Settings
from sapdlm.credentials import CredentialStore

BODY = b"P" * 400_000  # 400 KB in 10 KB chunks -> with 30 ms delay ~ 1.2 s


class SlowFileHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        for i in range(0, len(BODY), 10_000):
            self.wfile.write(BODY[i : i + 10_000])
            self.wfile.flush()
            time.sleep(0.05)
        return

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def slow_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowFileHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class SlowPortalClient(DownloadClient):
    def __init__(self, comp_url):
        super().__init__(
            ConnectionConfig(sap_url="https://portal", username="u", password="p"))
        self._comp_url = comp_url

    def get_object_version(self, dockey):
        from sapdlm.client.basket import ObjectVersion

        return ObjectVersion(global_status=0, reported_version=7)

    def start_download(self, dockey):
        return DownloadStart(
            dockey=dockey,
            components=[DownloadComponent(name="DOWNLOAD0_slow.bin",
                                          url=self._comp_url)],
        )

    def end_download(self, dockey):
        from sapdlm.client.basket import DownloadEnd

        return DownloadEnd(basket_status=4, error=False)


def test_ws_progress_events_during_download(tmp_path, monkeypatch, slow_server):
    # localhost URL would not pass the SAP allowlist - bypass for this test
    monkeypatch.setattr("sapdlm.engine.manager.is_valid_download_url", lambda u: True)

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    save_settings(data_dir, Settings(sap_url="https://portal", username="u",
                                     download_store=str(tmp_path / "store")))
    CredentialStore(data_dir).set("sap_password", "p")

    service = Service(data_dir=data_dir, download_dir=tmp_path / "store")
    service.manager.client = SlowPortalClient(slow_server + "/file.bin")
    service.settings.download_store = str(tmp_path / "store")

    app = create_app(service_factory=lambda data_dir, download_dir: service)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "hello"

            resp = client.post("/api/downloads/start", json={"keys": ["K1"]})
            assert resp.status_code == 202
            task_id = resp.json()["task_id"]

            progress_frames = []
            for _ in range(50):  # frames keep flowing; stop on final status
                frame = ws.receive_json()
                if frame.get("type") == "progress":
                    progress_frames.append(frame)
                elif (frame.get("type") == "status"
                        and frame.get("downloading") == []
                        and progress_frames):
                    break  # download finished and we saw progress

            assert progress_frames, "expected WebSocket progress events"
            bytes_seen = [f["bytes_committed"] for f in progress_frames]
            assert bytes_seen == sorted(bytes_seen)
            assert progress_frames[-1]["total_size"] == len(BODY)
            assert progress_frames[-1]["comp_total"] == 1
            task = client.get(f"/api/tasks/{task_id}").json()
            assert task["state"] == "done", task["error"]
    assert (tmp_path / "store" / "slow.bin").read_bytes() == BODY
