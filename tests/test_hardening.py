"""Hardening tests: path sanitization of portal-provided file names."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from sapdlm.client.basket import DownloadComponent, DownloadStart
from sapdlm.client.connection import ConnectionConfig
from sapdlm.client.download_client import DownloadClient
from sapdlm.engine.description import save_description
from sapdlm.engine.manager import DownloadManager, sanitize_file_name
from sapdlm.engine.state import ObjectRecord, StateStore

COMP_URL = "https://download.sap.com/comp.bin"


class StartOnlyClient(DownloadClient):
    """Portal stub returning a single component with an attacker-controlled name."""

    def get_object_version(self, dockey):
        from sapdlm.client.basket import ObjectVersion

        return ObjectVersion(global_status=0, reported_version=1)

    def start_download(self, dockey):
        return DownloadStart(
            dockey=dockey,
            components=[DownloadComponent(
                name="DOWNLOAD0_../../etc/evil.sar", url=COMP_URL)],
        )

    def end_download(self, dockey):
        from sapdlm.client.basket import DownloadEnd

        return DownloadEnd(basket_status=4, error=False)


def test_sanitize_file_name():
    assert sanitize_file_name("../../etc/evil.sar", "K1") == "evil.sar"
    assert sanitize_file_name("..\\..\\win.sar", "K1") == "win.sar"
    assert sanitize_file_name("..", "K1") == "K1"
    assert sanitize_file_name("plain.sar", "K1") == "plain.sar"


def test_traversal_confined_to_store(tmp_path):
    client = StartOnlyClient(
        ConnectionConfig(sap_url="https://portal", username="u", password="p"))
    store = StateStore(tmp_path / "app.db")
    store.upsert_object(ObjectRecord(dockey="K1", text="t"))

    # File server: any request gets a small body.
    def handler(request):
        return httpx.Response(200, content=b"DATA")

    manager = DownloadManager(
        store, client, tmp_path / "store",
        component_client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(handler)),
    )
    assert manager.download_object("K1") is True
    # File landed inside the store, nothing escaped it
    assert (tmp_path / "store" / "evil.sar").exists()
    assert not (tmp_path / "etc").exists()
    assert not (tmp_path / "evil.sar").exists()


def test_description_traversal_confined(tmp_path):
    target = save_description(
        tmp_path, "K1", ["line"], use_subdir=False,
        first_component_file="../../../etc/passwd",
    )
    # basename only: the file is written inside the store directory
    assert target.parent == tmp_path
    assert target.name == "passwd"
    assert not (tmp_path.parent / "etc").exists()
