"""Per-object download state machine (mirrors DownloadHandler of the official client; see docs/PROTOCOL.md).

Flow: OBJECT_VERSION -> validate/prepare local data -> DOWNLOAD_START2 ->
component downloads (resumable) -> description -> DOWNLOAD_END (BASKETSTATUS=4)
-> history entry. Cancellation via stop events leaves resumable state.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import httpx

from sapdlm.client.basket import DownloadStart
from sapdlm.client.connection import ConnectionConfig
from sapdlm.client.constants import FileStatus, LocalStatus
from sapdlm.client.download_client import DownloadClient
from sapdlm.client.parsers import is_valid_download_url
from sapdlm.engine.component import (
    ComponentDownloadError,
    ComponentDownloader,
    ComponentProgress,
)
from sapdlm.engine.description import save_description
from sapdlm.engine.state import (
    ENTRYTYPE_EXCEPTION,
    ComponentRecord,
    StateStore,
)


class DownloadAbort(Exception):
    """Fatal error while processing an object (mirrors mbTreatAsFatal)."""


def sanitize_file_name(name: str, fallback: str) -> str:
    """Hardening: portal-provided file names are confined to a single path
    segment inside the download store (no traversal)."""
    name = name.replace("\\", "/").split("/")[-1]
    if name in ("", ".", ".."):
        return fallback
    return name


class DownloadManager:
    def __init__(
        self,
        store: StateStore,
        client: DownloadClient,
        download_store: Path,
        use_subdir: bool = False,
        save_description_files: bool = True,
        component_client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self.store = store
        self.client = client
        self.download_store = Path(download_store)
        self.use_subdir = use_subdir
        self.save_description_files = save_description_files
        self._component_client_factory = component_client_factory
        self._stop_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # --------------------------------------------------------------- public API

    def is_downloading(self, dockey: str) -> bool:
        with self._lock:
            return dockey in self._stop_events

    @property
    def downloading_keys(self) -> list[str]:
        with self._lock:
            return list(self._stop_events)

    def stop(self, dockey: str) -> bool:
        """Pause a running object download (resumable)."""
        with self._lock:
            event = self._stop_events.get(dockey)
        if event:
            event.set()
            return True
        return False

    def download_object(
        self,
        dockey: str,
        progress: Callable[[ComponentProgress], None] | None = None,
    ) -> bool:
        """Run the object state machine to completion. Returns True if the
        object download completed, False if stopped by the caller."""
        stop = threading.Event()
        with self._lock:
            self._stop_events[dockey] = stop
        try:
            return self._process(dockey, stop, progress)
        finally:
            with self._lock:
                self._stop_events.pop(dockey, None)

    # ---------------------------------------------------------------- state flow

    def _process(
        self,
        dockey: str,
        stop: threading.Event,
        progress: Callable[[ComponentProgress], None] | None,
    ) -> bool:
        try:
            info = self.client.get_object_version(dockey)
            # Only the global status is stored here; local_version is set after
            # the version check (original: setLocalVersion after prepareLocalObjects).
            self.store.patch_object(dockey, global_status=info.global_status)
        except Exception as exc:
            self._protocol(dockey, str(exc), ENTRYTYPE_EXCEPTION)
            raise DownloadAbort(f"object info failed: {exc}") from exc

        if stop.is_set():
            return False

        self._prepare_local_objects(dockey, info.reported_version)
        self.store.patch_object(dockey, local_version=info.reported_version)

        if stop.is_set():
            return False

        try:
            start = self.client.start_download(dockey)
        except Exception as exc:
            self._protocol(dockey, str(exc), ENTRYTYPE_EXCEPTION)
            raise DownloadAbort(f"download start failed: {exc}") from exc

        if not self._validate_start(dockey, start):
            raise DownloadAbort("The SAP Support Portal returned an error while "
                                "trying to start the download")

        components = self._sync_components(dockey, start)
        self.store.patch_object(dockey, local_status=LocalStatus.PARTIAL)

        comp_client = (
            self._component_client_factory()
            if self._component_client_factory
            else self._build_component_client()
        )
        downloader = ComponentDownloader(self.store, comp_client, self.download_store)
        components = self.store.components(dockey)
        total = len(components)
        completed = 0

        for comp in components:
            if stop.is_set():
                self.store.add_protocol(dockey, "The object download was paused.")
                return False
            if comp.file_status == FileStatus.COMPLETED:
                completed += 1
                continue
            self.store.add_protocol(
                dockey, f"Downloading component {comp.resource_url}"
            )
            nr, n_total = completed + 1, total
            try:
                done = downloader.download(
                    comp, dockey, self.use_subdir, stop,
                    progress=self._wrap_progress(progress, nr, n_total),
                )
            except ComponentDownloadError as exc:
                self._protocol(dockey, str(exc), ENTRYTYPE_EXCEPTION)
                raise DownloadAbort(f"component download failed: {exc}") from exc
            except OSError as exc:
                self._protocol(dockey, str(exc), ENTRYTYPE_EXCEPTION)
                raise DownloadAbort(
                    "Unable to write to the target file. Check Download Store "
                    "directory configuration and try again."
                ) from exc
            if not done:
                return False  # paused
            completed += 1
            if comp.resource_url and all(
                c.file_status == FileStatus.COMPLETED
                for c in self.store.components(dockey)
            ):
                self.store.patch_object(dockey, local_status=LocalStatus.DOWNLOADED)

        if stop.is_set():
            self.store.add_protocol(dockey, "The object download was paused.")
            return False

        # Description
        if self.save_description_files and start.description:
            try:
                first = components[0].file_name if components else ""
                desc_path = save_description(
                    self.download_store, dockey, start.description,
                    self.use_subdir, first,
                )
                self.store.patch_object(dockey, desc_file=str(desc_path))
            except (OSError, ValueError) as exc:
                self._protocol(dockey, str(exc), ENTRYTYPE_EXCEPTION)
                raise DownloadAbort(f"saving the description failed: {exc}") from exc

        # DOWNLOAD_END
        if all(c.file_status == FileStatus.COMPLETED for c in self.store.components(dockey)):
            return self._finish(dockey, start)
        self.store.add_protocol(dockey, "The object download was paused.")
        return False

    def _finish(self, dockey: str, start: DownloadStart) -> bool:
        self.store.add_protocol(
            dockey, "Updating Object Status in your Download Basket ..."
        )
        try:
            end = self.client.end_download(dockey)
        except Exception as exc:
            self._protocol(dockey, str(exc), ENTRYTYPE_EXCEPTION)
            raise DownloadAbort(f"download end failed: {exc}") from exc
        if end.error:
            self._protocol(
                dockey,
                "Unable to save the download end status in the SAP Support Portal.",
                ENTRYTYPE_EXCEPTION,
            )
            raise DownloadAbort(
                "Unable to save the download end status in the SAP Support Portal."
            )
        record = self.store.get_object(dockey)
        self.store.patch_object(dockey, local_status=LocalStatus.COMPLETE)
        if record is not None:
            self.store.add_history(
                dockey, record.text, record.doctype, record.local_version
            )
        self.store.add_protocol(
            dockey, "The object download completed successfully."
        )
        return True

    # ----------------------------------------------------------------- helpers

    def _protocol(self, dockey: str, message: str, entry_type: int) -> None:
        self.store.add_protocol(dockey, message, entry_type)

    def _validate_start(self, dockey: str, start: DownloadStart) -> bool:
        if start.dockey and start.dockey.lower() != dockey.lower():
            return False
        for comp in start.components:
            if not is_valid_download_url(comp.url):
                self._protocol(
                    dockey, f"Failed to validate URL: {comp.url}",
                    ENTRYTYPE_EXCEPTION,
                )
                return False
        return True

    def _sync_components(self, dockey: str, start: DownloadStart) -> list[ComponentRecord]:
        base = self.client.config.effective_url().rstrip("/")
        records = []
        for i, comp in enumerate(start.components):
            url = comp.url
            if not url.startswith(("http://", "https://")):
                url = str(httpx.URL(base).join(url))
            pos = comp.name.find("_")
            file_name = dockey if pos == 0 else comp.name[pos + 1 :]
            file_name = sanitize_file_name(file_name, dockey)
            records.append(
                ComponentRecord(
                    id=None, dockey=dockey, seq=i, file_name=file_name,
                    resource_url=url, token=comp.token,
                )
            )
        return self.store.sync_components(dockey, records)

    def _prepare_local_objects(self, dockey: str, reported_version: int) -> None:
        """Version check + trusted-partial handling (checkLocalObject /
        prepareTrustedPredownloadedFile parity)."""
        components = self.store.components(dockey)
        if not components:
            return
        record = self.store.get_object(dockey)
        if reported_version != 0:
            if record and record.local_version != reported_version:
                self.store.add_protocol(
                    dockey,
                    "Object version mismatch. The predownloaded data is invalid.",
                )
                self._reset_local_data(dockey)
        else:
            self.store.add_protocol(
                dockey,
                "SAP Support Portal reported an invalid object version.",
                ENTRYTYPE_EXCEPTION,
            )
            raise DownloadAbort("SAP Support Portal reported an invalid object version.")

        for comp in self.store.components(dockey):
            if comp.file_status == FileStatus.COMPLETED:
                continue
            if not comp.local_file_name:
                continue
            path = Path(comp.local_file_name)
            if not path.exists():
                self.store.add_protocol(
                    dockey,
                    "The predownloaded data was deleted. "
                    "Restarting at byte position 0.",
                )
                self.store.patch_component(
                    comp, local_file_name="", bytes_committed=0,
                    file_status=FileStatus.NOT_STARTED,
                )
            elif path.stat().st_size == comp.bytes_committed:
                continue
            elif path.stat().st_size > comp.bytes_committed:
                # Truncate to the trusted committed length.
                with open(path, "rb") as src:
                    data = src.read(comp.bytes_committed)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
            else:
                path.unlink(missing_ok=True)
                self.store.patch_component(
                    comp, local_file_name="", bytes_committed=0,
                    file_status=FileStatus.NOT_STARTED,
                )

    def _reset_local_data(self, dockey: str) -> None:
        """Version-mismatch reset: drop files and component state but keep the
        object record (basket metadata is still needed for the history entry)."""
        for comp in self.store.components(dockey):
            if comp.local_file_name:
                Path(comp.local_file_name).unlink(missing_ok=True)
        self.store.delete_object_components(dockey)
        self.store.patch_object(
            dockey, local_status=LocalStatus.NOT_STARTED, local_version=0,
            desc_file="", dir_created=0,
        )

    def delete_local_data(self, dockey: str) -> None:
        """Delete partially/completely downloaded object data + state
        (DownloadClient.deleteLocalObjectData parity)."""
        record = self.store.get_object(dockey)
        for comp in self.store.components(dockey):
            if comp.local_file_name:
                Path(comp.local_file_name).unlink(missing_ok=True)
        if record and record.dir_created and self.use_subdir:
            obj_dir = self.download_store / dockey
            if obj_dir.is_dir():
                try:
                    obj_dir.rmdir()
                except OSError:
                    pass
        if record and record.desc_file:
            Path(record.desc_file).unlink(missing_ok=True)
        self.store.delete_object(dockey)

    def _build_component_client(self) -> httpx.Client:
        """Component downloads inherit auth/proxy from the portal connection."""
        config: ConnectionConfig = self.client.config
        kwargs: dict = {"timeout": httpx.Timeout(30.0, read=None),
                        "follow_redirects": True}
        if config.use_proxy and config.proxy_host:
            auth = (
                f"{config.proxy_username}:{config.proxy_password}"
                if config.proxy_username else None
            )
            kwargs["proxy"] = (
                f"http://{auth + '@' if auth else ''}"
                f"{config.proxy_host}:{config.proxy_port}"
            )
        if config.use_cert_auth:
            kwargs["cert"] = config.cert_path
        else:
            kwargs["auth"] = (config.username, config.password)
        return httpx.Client(**kwargs)

    def _wrap_progress(
        self,
        progress: Callable[[ComponentProgress], None] | None,
        nr: int,
        total: int,
    ) -> Callable[[ComponentProgress], None] | None:
        if progress is None:
            return None

        def wrap(update: ComponentProgress) -> None:
            update.comp_nr = nr
            update.comp_total = total
            progress(update)

        return wrap
