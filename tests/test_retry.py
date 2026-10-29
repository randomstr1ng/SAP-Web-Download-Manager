"""Tests for per-component download retries with exponential backoff."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from sapdlm.client.basket import (
    DownloadComponent,
    DownloadEnd,
    DownloadStart,
    ObjectVersion,
)
from sapdlm.client.connection import ConnectionConfig
from sapdlm.client.constants import LocalStatus
from sapdlm.client.download_client import DownloadClient
from sapdlm.engine.manager import DownloadAbort, DownloadManager
from sapdlm.engine.state import ObjectRecord, StateStore

COMP_URL = "https://download.sap.com/comp.bin"
BODY = b"R" * 200_000


class RetryPortalClient(DownloadClient):
    def get_object_version(self, dockey):
        return ObjectVersion(global_status=0, reported_version=3)

    def start_download(self, dockey):
        return DownloadStart(
            dockey=dockey,
            components=[DownloadComponent(name="DOWNLOAD0_comp.bin", url=COMP_URL, token="t=1")],
        )

    def end_download(self, dockey):
        return DownloadEnd(basket_status=4, error=False)


def flaky_handler_factory(fail_times: int):
    """HTTP handler that fails the first N requests with a 503, then serves."""
    state = {"failures": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["failures"] < fail_times:
            state["failures"] += 1
            return httpx.Response(503)
        if request.headers.get("Range"):
            return httpx.Response(
                206,
                content=BODY,
                headers={"Content-Range": f"bytes 0-{len(BODY) - 1}/{len(BODY)}"},
            )
        return httpx.Response(200, content=BODY)

    return handler


def make_manager(tmp_path: Path, handler, retries: int) -> DownloadManager:
    config = ConnectionConfig(sap_url="https://portal", username="u", password="p")
    store = StateStore(tmp_path / "app.db")
    store.upsert_object(ObjectRecord(dockey="K1", text="t"))
    return DownloadManager(
        store,
        RetryPortalClient(config),
        tmp_path / "store",
        component_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        component_retries=retries,
    )


def test_transient_error_retried_and_completed(tmp_path, monkeypatch):
    handler = flaky_handler_factory(fail_times=2)
    # keep the test fast: no real sleeping between retries
    sleeps = []
    monkeypatch.setattr("sapdlm.engine.manager.time.sleep", lambda s: sleeps.append(s))
    manager = make_manager(tmp_path, handler, retries=3)

    assert manager.download_object("K1") is True
    assert (tmp_path / "store" / "comp.bin").read_bytes() == BODY
    assert manager.store.get_object("K1").local_status == LocalStatus.COMPLETE
    # two failures -> two backoff sleeps
    assert sleeps == [2, 4]
    messages = [p.message for p in manager.store.protocol("K1")]
    assert any("retrying in 2 s (attempt 1/3)" in m for m in messages)


def test_retries_exhausted_aborts(tmp_path, monkeypatch):
    handler = flaky_handler_factory(fail_times=99)  # always 503
    monkeypatch.setattr("sapdlm.engine.manager.time.sleep", lambda s: None)
    manager = make_manager(tmp_path, handler, retries=2)

    with pytest.raises(DownloadAbort, match="after 3 attempts"):
        manager.download_object("K1")
    messages = [p.message for p in manager.store.protocol("K1")]
    assert any("retrying in" in m for m in messages)
    assert manager.store.get_object("K1").local_status != LocalStatus.COMPLETE


def test_client_error_not_retried(tmp_path):
    """4xx (auth/permission) must fail immediately without retry attempts."""

    def handler(request):
        return httpx.Response(403)

    manager = make_manager(tmp_path, handler, retries=3)
    with pytest.raises(DownloadAbort, match="403"):
        manager.download_object("K1")
    # no retry protocol entries (would contain "retrying")
    messages = [p.message for p in manager.store.protocol("K1")]
    assert not any("retrying" in m for m in messages)
