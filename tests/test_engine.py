"""Engine tests: state store, resumable component downloads, and the
per-object state machine against stubbed portal responses."""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest

from sapdlm.client.basket import DownloadComponent, DownloadEnd, DownloadStart, ObjectVersion
from sapdlm.client.connection import ConnectionConfig
from sapdlm.client.constants import FileStatus, LocalStatus, MAX_HISTORY_ENTRIES
from sapdlm.client.download_client import DownloadClient
from sapdlm.engine.description import save_description
from sapdlm.engine.manager import DownloadAbort, DownloadManager
from sapdlm.engine.state import (
    ENTRYTYPE_EXCEPTION,
    ComponentRecord,
    ObjectRecord,
    StateStore,
)

COMPONENT_BODY = b"A" * 200_000
COMP_URL = "https://download.sap.com/kernels/749_LINUX_X86_64.SAR"


class FakePortalClient(DownloadClient):
    """Portal stub - canned OBJECT_VERSION / DOWNLOAD_START2 / DOWNLOAD_END."""

    def __init__(self, reported_version=5, end_error=False, components=None):
        super().__init__(
            ConnectionConfig(sap_url="https://portal.example.com", username="u", password="p")
        )
        self.reported_version = reported_version
        self.end_error = end_error
        self.components = components or [
            ("DOWNLOAD0_749_LINUX_X86_64.SAR", COMP_URL, "SAPSSO=tok")
        ]
        self.end_called = False

    def get_object_version(self, dockey):
        return ObjectVersion(global_status=0, reported_version=self.reported_version)

    def start_download(self, dockey):
        start = DownloadStart(dockey=dockey)
        for name, url, token in self.components:
            from sapdlm.client.basket import DownloadComponent

            start.components.append(DownloadComponent(name=name, url=url, token=token))
        start.description = ["SAP Kernel 7.49"]
        return start

    def end_download(self, dockey):
        self.end_called = True
        return DownloadEnd(basket_status=5 if self.end_error else 4,
                           error=self.end_error)


def range_aware_handler(request: httpx.Request) -> httpx.Response:
    """Stub file server honoring Range requests."""
    if request.url.host != "download.sap.com":
        return httpx.Response(404)
    if request.url.path == "/deny.sar":
        return httpx.Response(200, content=b"never mind")
    range_header = request.headers.get("Range", "")
    if range_header:
        start = int(range_header.split("bytes=")[1].split("-")[0])
        if request.url.path == "/no-resume.sar":
            return httpx.Response(200, content=COMPONENT_BODY)  # ignores Range
        body = COMPONENT_BODY[start:]
        return httpx.Response(
            206,
            content=body,
            headers={
                "Content-Range": f"bytes {start}-{len(COMPONENT_BODY) - 1}/{len(COMPONENT_BODY)}",
                "Content-Length": str(len(body)),
            },
        )
    return httpx.Response(
        200, content=COMPONENT_BODY,
        headers={"Content-Length": str(len(COMPONENT_BODY))},
    )


def make_manager(tmp_path: Path, client: DownloadClient, **kwargs) -> DownloadManager:
    store = StateStore(tmp_path / "app.db")
    store.upsert_object(ObjectRecord(dockey="K1", text="Kernel", doctype="SAR",
                                     size_kb=200))
    factory = kwargs.pop(
        "component_client_factory",
        lambda: httpx.Client(transport=httpx.MockTransport(range_aware_handler)),
    )
    return DownloadManager(
        store, client, tmp_path / "store", component_client_factory=factory, **kwargs
    )


# ------------------------------------------------------------------ state store

def test_history_cap(tmp_path):
    store = StateStore(tmp_path / "cap.db")
    from sapdlm.client.constants import MAX_HISTORY_ENTRIES

    for i in range(MAX_HISTORY_ENTRIES + 10):
        store.add_history(f"K{i}", "t", "SAR", 1)
    assert len(store.history()) == MAX_HISTORY_ENTRIES
    assert store.history()[0].dockey == f"K{MAX_HISTORY_ENTRIES + 9}"


def test_sync_components_resets_on_url_change(tmp_path):
    store = StateStore(tmp_path / "sync.db")
    record = ObjectRecord(
        dockey="K1"
    )
    store.upsert_object(record)
    from sapdlm.engine.state import ComponentRecord

    store.sync_components("K1", [
        ComponentRecord(id=None, dockey="K1", seq=0, file_name="a.sar",
                        resource_url="https://download.sap.com/a.sar", token="t=1"),
    ])
    comps = store.components("K1")
    assert len(comps) == 1
    # simulate progress
    store.patch_component(comps[0], bytes_committed=100, file_status=FileStatus.RUNNING)
    # same URL -> progress kept
    comps = store.sync_components("K1", [
        ComponentRecord(id=None, dockey="K1", seq=0, file_name="a.sar",
                        resource_url="https://download.sap.com/a.sar", token="t=1"),
    ])
    assert comps[0].bytes_committed == 100
    # changed URL -> progress reset
    comps = store.sync_components("K1", [
        ComponentRecord(id=None, dockey="K1", seq=0, file_name="a.sar",
                        resource_url="https://download.sap.com/a2.sar", token="t=1"),
    ])
    assert comps[0].bytes_committed == 0


# ------------------------------------------------------------ full object download

def test_full_download_success(tmp_path):
    manager = make_manager(tmp_path, FakePortalClient())
    assert manager.download_object("K1") is True
    store = manager.store
    assert store.get_object("K1").local_status == LocalStatus.COMPLETE
    assert manager.client.end_called is True
    files = list((tmp_path / "store").glob("*"))
    assert any(f.name == "749_LINUX_X86_64.SAR" for f in files)
    target = tmp_path / "store" / "749_LINUX_X86_64.SAR"
    assert target.stat().st_size == len(COMPONENT_BODY)
    assert len(store.history()) == 1
    messages = [p.message for p in store.protocol("K1")]
    assert any("completed successfully" in m for m in messages)
    # description saved (store dir, no subdir)
    assert (tmp_path / "store" / "K1").exists() or (tmp_path / "store" / "749_LINUX_X86_64").exists()


def test_download_end_failure_no_history(tmp_path):
    manager = make_manager(tmp_path, FakePortalClient(end_error=True))
    with pytest.raises(DownloadAbort):
        manager.download_object("K1")
    # files downloaded but basket status not confirmed
    assert manager.store.get_object("K1").local_status != LocalStatus.COMPLETE
    assert len(manager.store.history()) == 0
    messages = [p.message for p in manager.store.protocol("K1")]
    assert any("Unable to save the download end status" in m for m in messages)


def test_allowlist_rejection(tmp_path):
    client = FakePortalClient(components=[("DOWNLOAD0_evil.bin",
                                          "https://evil.example.com/x.bin", "")])
    manager = make_manager(tmp_path, client)
    with pytest.raises(DownloadAbort):
        manager.download_object("K1")
    protocol = manager.store.protocol("K1")
    assert any("Failed to validate URL" in p.message for p in protocol)
    assert any(p.type == ENTRYTYPE_EXCEPTION for p in protocol)


# ------------------------------------------------------------------- pause/resume

def test_pause_and_resume(tmp_path):
    manager = make_manager(tmp_path, FakePortalClient())
    store = manager.store
    # Simulate a mid-download state: .part file with committed bytes.
    part_dir = tmp_path / "store"
    part_dir.mkdir(exist_ok=True)
    part = part_dir / "749_LINUX_X86_64.SAR.part"
    part.write_bytes(COMPONENT_BODY[:100_000])
    comps = store.sync_components("K1", [
        ComponentRecord(
            id=None, dockey="K1", seq=0, file_name="749_LINUX_X86_64.SAR",
            resource_url=COMP_URL, token="SAPSSO=tok",
            local_file_name=str(part), bytes_committed=100_000,
            file_status=FileStatus.RUNNING,
        )
    ])
    store.patch_object("K1", local_version=5, local_status=LocalStatus.PARTIAL)

    assert manager.download_object("K1") is True
    target = part_dir / "749_LINUX_X86_64.SAR"
    assert target.stat().st_size == len(COMPONENT_BODY)
    assert target.read_bytes() == COMPONENT_BODY
    assert store.components("K1")[0].file_status == FileStatus.COMPLETED
    assert store.get_object("K1").local_status == LocalStatus.COMPLETE


def test_resume_not_supported_restarts(tmp_path):
    client = FakePortalClient(components=[("DOWNLOAD0_nr.sar",
                                           "https://download.sap.com/no-resume.sar", "")])
    manager = make_manager(tmp_path, client)
    store = manager.store
    part = tmp_path / "store" / "nr.sar.part"
    part.parent.mkdir(exist_ok=True)
    part.write_bytes(COMPONENT_BODY[:50_000])
    store.sync_components("K1", [
        ComponentRecord(
            id=None, dockey="K1", seq=0, file_name="nr.sar",
            resource_url="https://download.sap.com/no-resume.sar", token="",
            local_file_name=str(part), bytes_committed=50_000,
            file_status=FileStatus.RUNNING,
        )
    ])
    store.patch_object("K1", local_version=5)
    assert manager.download_object("K1") is True
    target = tmp_path / "store" / "nr.sar"
    assert target.read_bytes() == COMPONENT_BODY


def test_version_mismatch_cleans_state(tmp_path):
    manager = make_manager(tmp_path, FakePortalClient(reported_version=9))
    store = manager.store
    stale = tmp_path / "store" / "stale.sar.part"
    stale.parent.mkdir(exist_ok=True)
    stale.write_bytes(b"OLD")
    store.sync_components("K1", [
        ComponentRecord(
            id=None, dockey="K1", seq=0, file_name="749_LINUX_X86_64.SAR",
            resource_url=COMP_URL, token="t",
            local_file_name=str(stale), bytes_committed=3,
            file_status=FileStatus.RUNNING,
        )
    ])
    store.patch_object("K1", local_version=5)
    assert manager.download_object("K1") is True
    assert not stale.exists()  # old partial deleted on version mismatch
    assert (tmp_path / "store" / "749_LINUX_X86_64.SAR").exists()


def test_stop_event_pauses(tmp_path):
    manager = make_manager(tmp_path, FakePortalClient())
    stop = threading.Event()
    stop.set()  # stopped before any component work
    assert manager._process("K1", stop, None) is False
    assert manager.store.get_object("K1").local_status != LocalStatus.COMPLETE
    assert not manager.client.end_called


# -------------------------------------------------------------------- description

def test_description_binary_patches(tmp_path):
    desc = [
        "{1F3AAA90-4782-4632-8C5E-E92F7A7751E4}=BINARY_PATCHES",
        "AArowstartBB rowstartCC",
    ]
    target = save_description(tmp_path, "K1", desc, use_subdir=False,
                              first_component_file="kernel.sar")
    assert target.name == "kernel"
    assert target.read_text() == "AA\nBB \nCC"


def test_description_plain(tmp_path):
    target = save_description(tmp_path, "K1", ["line1", "line2"], use_subdir=True,
                              first_component_file="kernel.sar")
    assert target == tmp_path / "K1" / "kernel"
    assert target.read_text() == "line1\nline2"
