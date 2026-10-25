"""Single-file component downloader (LocalStatusFile.downloadLocalFile parity).

Semantics mirrored from the official Java client (see docs/PROTOCOL.md):
- Token is sent as a ``Cookie`` header.
- Resume: if bytes are committed, a probe GET is made first, then a second GET
  with ``Range: bytes=<offset>-<total>``. ``206`` -> append to ``<name>.part``;
  ``200`` -> server ignores resume -> delete the partial file and start fresh.
- Total size is derived from Content-Range (total) preferring it over
  Content-Length.
- State is checkpointed after each flushed buffer (500 KB) so a kill loses at
  most ~500 KB.
- On completion the ``.part`` file is renamed (suffix ``.1``, ``.2``... on
  collision).
- Cancellation via a ``threading.Event`` leaves a resumable ``.part`` file.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from sapdlm.engine.state import ComponentRecord, StateStore

FLUSH_BYTES = 500_000      # 50 buffers x 10 KB in the original
CHUNK = 65_536


@dataclass
class ComponentProgress:
    dockey: str
    seq: int
    file_name: str
    bytes_committed: int
    total_size: int
    speed_bps: float
    source_host: str
    comp_nr: int
    comp_total: int


class ComponentDownloadError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ComponentDownloader:
    def __init__(
        self,
        store: StateStore,
        client: httpx.Client,
        download_store: Path,
    ):
        self.store = store
        self.client = client
        self.download_store = Path(download_store)

    # ----------------------------------------------------------------- helpers

    def _target_dir(self, dockey: str, use_subdir: bool) -> Path:
        return self.download_store / dockey if use_subdir else self.download_store

    # -------------------------------------------------------------------- main

    def download(
        self,
        comp: ComponentRecord,
        dockey: str,
        use_subdir: bool,
        stop: threading.Event,
        progress=None,  # Callable[[ComponentProgress], None]
    ) -> bool:
        """Download one component. Returns True when completed, False when
        stopped by the caller (state saved, resumable)."""
        headers: dict[str, str] = {}
        if comp.token:
            headers["Cookie"] = comp.token

        start_offset = comp.bytes_committed if comp.bytes_committed > 0 else 0
        response = self._get(comp.resource_url, headers)
        if start_offset > 0:
            total_hint = response.content_length()
            headers["Range"] = f"bytes={start_offset}-{total_hint if total_hint else ''}".rstrip("-")
            response = self._get(comp.resource_url, headers)

        total_size = response.content_length()

        if response.status_code == 200 and start_offset > 0:
            # Server does not support resume: delete local file, restart at 0.
            self._delete_local(comp)
            start_offset = 0
        elif response.status_code == 206:
            pass  # resuming
        elif response.status_code >= 400:
            raise ComponentDownloadError(
                f"The request failed: {response.status_code}", response.status_code
            )
        elif start_offset == 0:
            pass  # fresh 2xx download
        else:
            raise ComponentDownloadError(
                f"Unexpected response status while resuming: {response.status_code}",
                response.status_code,
            )

        target_dir = self._target_dir(dockey, use_subdir)
        final_path = target_dir / comp.file_name
        if not comp.local_file_name:
            if not target_dir.exists():
                target_dir.mkdir(parents=True, exist_ok=True)
                if use_subdir:
                    self.store.patch_object(dockey, dir_created=True)
            part_path = str(final_path) + ".part"
            self.store.patch_component(comp, local_file_name=part_path, file_status=1)
            comp.local_file_name = part_path
        else:
            part_path = comp.local_file_name
            final_path = (
                Path(part_path[:-5]) if part_path.endswith(".part") else Path(part_path)
            )

        comp.file_status = 1
        self.store.patch_component(comp, file_status=1)

        mode = "ab" if start_offset > 0 else "wb"
        bytes_committed = start_offset
        started = time.monotonic()
        last_report = 0.0
        last_checkpoint = 0
        finished = False

        iterator = response.stream()
        try:
            with open(part_path, mode) as fh:
                while not stop.is_set():
                    chunk = next(iterator, None)
                    if not chunk:
                        finished = True
                        break
                    fh.write(chunk)
                    bytes_committed += len(chunk)
                    comp.bytes_committed = bytes_committed
                    if fh.tell() - last_checkpoint >= FLUSH_BYTES:
                        fh.flush()
                        last_checkpoint = fh.tell()
                        self.store.patch_component(
                            comp, bytes_committed=bytes_committed
                        )
                    now = time.monotonic()
                    if progress and now - last_report >= 1.0:
                        last_report = now
                        elapsed = max(now - started, 1e-6)
                        progress(ComponentProgress(
                            dockey=dockey, seq=comp.seq, file_name=comp.file_name,
                            bytes_committed=bytes_committed, total_size=total_size,
                            speed_bps=(bytes_committed - start_offset) / elapsed,
                            source_host=_host(comp.resource_url),
                            comp_nr=0, comp_total=0,
                        ))
        finally:
            response.close()

        if stop.is_set() and not finished:
            self.store.add_protocol(dockey, "The object download was paused.")
            return False

        if total_size and bytes_committed < total_size:
            raise ComponentDownloadError(
                "The connection was closed before all file data was transferred."
            )

        # Rename .part -> final name with collision suffixes (.1, .2, ...).
        trial = 0
        while True:
            candidate = final_path if trial == 0 else Path(f"{final_path}.{trial}")
            if not candidate.exists():
                break
            trial += 1
        Path(part_path).rename(candidate)

        self.store.patch_component(
            comp, local_file_name=str(candidate), file_name=candidate.name,
            bytes_committed=bytes_committed, file_status=2,
        )
        comp.local_file_name = str(candidate)
        comp.file_name = candidate.name
        comp.file_status = 2
        return True

    # ------------------------------------------------------------------ plumbing

    def _get(self, url: str, headers: dict[str, str]):
        if not url.startswith(("http://", "https://")):
            raise ComponentDownloadError(f"The URL '{url}' is invalid.")
        try:
            response = self.client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ComponentDownloadError(
                f"Unable to read from the input stream: {exc}"
            ) from exc
        return _StreamedResponse(response)

    def _delete_local(self, comp: ComponentRecord) -> None:
        if comp.local_file_name:
            path = Path(comp.local_file_name)
            if path.exists():
                path.unlink()
        self.store.patch_component(
            comp, local_file_name="", bytes_committed=0, file_status=0
        )
        comp.local_file_name = ""
        comp.bytes_committed = 0
        comp.file_status = 0


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host.lower()
    except Exception:
        return ""


class _StreamedResponse:
    """Adapts httpx.Response to the small interface the downloader needs
    (status code, content_length(), stream())."""

    def __init__(self, response: httpx.Response):
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers

    def content_length(self) -> int:
        content_range = self.headers.get("Content-Range", "")
        if content_range:
            pos = content_range.rfind("/")
            if pos > 0:
                try:
                    return int(content_range[pos + 1 :])
                except ValueError:
                    pass
        try:
            return int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return 0

    def stream(self):
        return self._response.iter_bytes(chunk_size=CHUNK)

    def close(self) -> None:
        self._response.close()
