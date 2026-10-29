"""Service layer: wires config, credentials, client, engine and the event bus.

All portal/engine operations run in worker threads (the engine is synchronous);
FastAPI endpoints schedule them and report via tasks + WebSocket events.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from sapdlm import DLM_VERSION, __version__
from sapdlm.client.basket import BasketContent
from sapdlm.client.connection import ConnectionConfig
from sapdlm.client.constants import FileStatus
from sapdlm.client.download_client import DownloadClient
from sapdlm.config import Settings, load_settings, save_settings
from sapdlm.credentials import CredentialStore
from sapdlm.engine.component import ComponentProgress
from sapdlm.engine.manager import DownloadManager
from sapdlm.engine.state import ObjectRecord, StateStore

logger = logging.getLogger("sapdlm")


class EventBus:
    """Fan-out event broadcast to connected WebSockets (thread-safe publish)."""

    def __init__(self) -> None:
        self._clients: set = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, websocket) -> None:
        self._clients.add(websocket)

    def unsubscribe(self, websocket) -> None:
        self._clients.discard(websocket)

    def publish(self, event: dict[str, Any]) -> None:
        """Safe to call from worker threads."""
        if self._loop is None:
            return

        def _schedule() -> None:
            # call_soon_threadsafe only accepts plain callbacks - the async
            # broadcast must be started as a task on the event loop.
            asyncio.ensure_future(self._broadcast(event))

        try:
            self._loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            pass

    async def _broadcast(self, event: dict[str, Any]) -> None:
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001 - a dead socket must not break the fan-out
                dead.append(ws)
        for ws in dead:
            self.unsubscribe(ws)


class TaskRegistry:
    """Track background tasks (refresh / downloads / connection tests)."""

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, detail: str = "") -> str:
        task_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._tasks[task_id] = {
                "id": task_id,
                "kind": kind,
                "detail": detail,
                "state": "running",
                "error": None,
                "created": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        return task_id

    def finish(self, task_id: str, error: str | None = None, state: str | None = None) -> None:
        with self._lock:
            if task_id in self._tasks:
                if state is None:
                    state = "error" if error else "done"
                self._tasks[task_id]["state"] = state
                self._tasks[task_id]["error"] = error

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            return self._tasks.get(task_id)


class Service:
    """Application state singleton used by all routes."""

    def __init__(
        self,
        data_dir: Path,
        download_dir: Path,
        http_client=None,
        component_client_factory=None,
        webhook_client=None,
    ):
        self.data_dir = Path(data_dir)
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.settings: Settings = load_settings(self.data_dir)
        if not self.settings.download_store:
            self.settings.download_store = str(self.download_dir)
        self.credentials = CredentialStore(self.data_dir)
        self.store = StateStore(self.data_dir / "app.db")
        self.events = EventBus()
        self.tasks = TaskRegistry()
        self._http_client = http_client
        self._webhook_client = webhook_client or httpx.Client(timeout=5.0)
        self.metrics = None
        self._component_client_factory = component_client_factory
        self.manager = DownloadManager(
            self.store,
            self._build_client(),
            self.download_dir,
            use_subdir=self.settings.create_subdir,
            save_description_files=self.settings.save_description,
            component_client_factory=component_client_factory,
            component_retries=self.settings.component_retries,
        )
        self.basket: BasketContent | None = None
        # Set during app lifespan (needs a running event loop).
        self.scheduler = None
        self.watcher = None
        self._refresh_stop = None
        self._apply_log_level()

    def _apply_log_level(self) -> None:
        """settings.trace parity: verbose logging for the sapdlm loggers."""
        logging.getLogger("sapdlm").setLevel(logging.DEBUG if self.settings.trace else logging.INFO)

    # ------------------------------------------------------------------ factories

    def _build_client(self) -> DownloadClient:
        return DownloadClient(self.connection_config(), http_client=self._http_client)

    def reload_engine(self) -> None:
        """Apply settings changes to the running engine in place. The
        DownloadClient instance is kept so busy/connected state survives a
        settings save (rebuilding it would always show 'not connected')."""
        manager = self.manager
        manager.client.config = self.connection_config()
        manager.download_store = Path(self.settings.download_store)
        manager.use_subdir = self.settings.create_subdir
        manager.save_description_files = self.settings.save_description
        manager.component_retries = max(0, self.settings.component_retries)

    def connection_config(self) -> ConnectionConfig:
        s = self.settings
        # Trim: autofilled form fields often carry stray spaces, which would
        # silently break basic authentication (username " S123..." != "S123...").
        return ConnectionConfig(
            sap_url=s.sap_url.strip(),
            username=s.username.strip(),
            password=self.credentials.get("sap_password"),
            use_cert_auth=s.use_cert_auth,
            cert_path=s.cert_path.strip(),
            cert_key_password=self.credentials.get("cert_key_password"),
            use_proxy=s.use_proxy,
            proxy_host=s.proxy_host.strip(),
            proxy_port=s.proxy_port,
            proxy_username=s.proxy_username.strip(),
            proxy_password=self.credentials.get("proxy_password"),
        )

    # ------------------------------------------------------------------- status

    def is_configured(self) -> bool:
        if not self.settings.is_complete():
            return False
        if self.settings.use_cert_auth:
            return self.credentials.stored("cert_key_password")
        return self.credentials.stored("sap_password")

    def status(self) -> dict:
        client = self.manager.client
        try:
            server = httpx.URL(self.settings.sap_url).host or ""
        except Exception:  # noqa: BLE001 - any URL parse failure just hides the host
            server = ""
        return {
            "status": "ok",
            "app_version": __version__,
            "dlm_protocol_version": DLM_VERSION,
            "configured": self.is_configured(),
            "connected": client.connected,
            "server": server,
            "busy": client.busy,
            "download_dir": self.settings.download_store,
            "downloading": self.manager.downloading_keys,
            "watching": bool(self.watcher is not None and self.watcher.status()["active"]),
        }

    # ------------------------------------------------------------------- basket

    def refresh_basket(self, stop=None) -> dict:
        """Synchronous basket fetch (run in a worker thread). `stop` is a
        threading.Event that cancels the fetch (between retries)."""
        self._refresh_stop = stop
        try:
            self.manager.client.config = self.connection_config()
            content = self.manager.client.check_basket_content(stop=stop)
        finally:
            self._refresh_stop = None
        self.basket = content
        for item in content.items.values():
            record = self.store.get_object(item.dockey) or ObjectRecord(dockey=item.dockey)
            record.text = item.text
            record.doctype = item.doc_type
            record.size_kb = item.size_kb
            record.global_status = item.global_status
            record.basket_status = item.basket_status
            self.store.upsert_object(record)
        self.events.publish({"type": "status", **self.status()})
        return self.basket_view()

    def cancel_refresh(self) -> bool:
        """Cancel a running basket refresh (if any)."""
        if self._refresh_stop is not None and not self._refresh_stop.is_set():
            self._refresh_stop.set()
            return True
        return False

    def basket_view(self) -> dict:
        """Current basket merged with local status."""
        if self.basket is None:
            return {
                "refreshed": False,
                "full_name": "",
                "items": [],
                "upgrade": None,
                "user_ip": "",
            }
        items = []
        for item in self.basket.items.values():
            record = self.store.get_object(item.dockey)
            local_status = record.local_status if record else None
            # Technical file names are known after a download attempt (from
            # DOWNLOAD_START2); before that only the dockey distinguishes
            # items with identical description texts.
            local_files = [c.file_name for c in self.store.components(item.dockey) if c.file_name]
            items.append(
                {
                    "dockey": item.dockey,
                    "text": item.text,
                    "doc_type": item.doc_type,
                    "size_kb": item.size_kb,
                    "size_text": item.size_text,
                    "reg_date": item.reg_date,
                    "reg_time": item.reg_time,
                    "global_status": item.global_status,
                    "basket_status": item.basket_status,
                    "local_status": local_status,
                    "local_files": local_files,
                    "status_text": item.status_text(
                        local_status,
                        downloading=self.manager.is_downloading(item.dockey),
                    ),
                    "downloading": self.manager.is_downloading(item.dockey),
                }
            )
        return {
            "refreshed": True,
            "full_name": self.basket.full_name,
            "user_ip": self.basket.user_ip,
            "upgrade": (
                self.basket.upgrade.__dict__
                if (self.basket.upgrade.version or self.basket.upgrade.url)
                else None
            ),
            "items": items,
        }

    def remove_from_basket(self, dockey: str) -> None:
        """Delete local data + OBJDEL on the portal (removeFromDownloadBasket)."""
        self.manager.client.config = self.connection_config()
        self.manager.delete_local_data(dockey)
        self.manager.client.remove_from_basket(dockey)
        if self.basket is not None:
            self.basket.items.pop(dockey, None)
        self.events.publish({"type": "status", **self.status()})

    # ---------------------------------------------------------------- downloads

    def download_keys(self, mode: str, keys: list[str] | None) -> list[str]:
        if mode == "all":
            if self.basket is not None:
                return list(self.basket.items.keys())
            return [r.dockey for r in self.store.list_objects()]
        return list(keys or [])

    def run_download(
        self, keys: list[str], progress: Callable[[ComponentProgress], None] | None = None
    ) -> None:
        """Process the given objects, up to max_concurrent_objects in parallel.
        Tracks metrics and sends webhook notifications per object outcome."""
        workers = max(1, self.settings.max_concurrent_objects)
        if workers == 1 or len(keys) <= 1:
            for dockey in keys:
                self._run_one(dockey, progress)
            return
        with ThreadPoolExecutor(max_workers=min(workers, len(keys))) as pool:
            futures = [pool.submit(self._run_one, dockey, progress) for dockey in keys]
            for future in futures:
                future.result()

    def _run_one(self, dockey: str, progress: Callable[[ComponentProgress], None] | None) -> None:
        """Download a single object (metrics + webhook on the outcome)."""
        self.events.publish({"type": "status", **self.status(), "action": f"Downloading {dockey}"})
        try:
            done = self.manager.download_object(dockey, progress=self._progress_publisher(progress))
            if done:
                metrics = getattr(self, "metrics", None)
                if metrics is not None:
                    metrics.track_object_finished(dockey, success=True)
                self._send_webhook("completed", dockey)
        except Exception as exc:  # noqa: BLE001 - engine aborts on this object, continue
            metrics = getattr(self, "metrics", None)
            if metrics is not None:
                metrics.track_object_finished(dockey, success=False)
            self._send_webhook("failed", dockey)
            logger.warning("download of %s failed: %s", dockey, exc)
        finally:
            self.events.publish({"type": "status", **self.status()})

    def _progress_publisher(
        self, progress: Callable[[ComponentProgress], None] | None
    ) -> Callable[[ComponentProgress], None]:
        def publish(update: ComponentProgress) -> None:
            metrics = getattr(self, "metrics", None)
            if metrics is not None:
                metrics.track_progress(update.dockey, update.bytes_committed)
            if progress is not None:
                progress(update)
            else:
                self.events.publish({"type": "progress", **vars(update)})

        return publish

    def _send_webhook(self, event: str, dockey: str) -> None:
        """POST a JSON notification to the configured webhook (if any).
        Fire-and-forget: errors are logged, never propagated."""
        url = self.settings.webhook_url
        if not url:
            return
        record = self.store.get_object(dockey)
        payload = {
            "event": event,
            "dockey": dockey,
            "text": record.text if record else "",
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        headers = {"Content-Type": "application/json"}
        secret = self.credentials.get("webhook_secret")
        if secret:
            headers["X-Webhook-Secret"] = secret
        try:
            self._webhook_client.post(url, json=payload, headers=headers, timeout=5)
        except Exception as exc:  # noqa: BLE001 - must never break downloads
            logger.warning("webhook delivery to %s failed: %s", url, exc)

    def pause_download(self, dockey: str) -> bool:
        return self.manager.stop(dockey)

    def delete_local_data(self, dockey: str) -> None:
        metrics = getattr(self, "metrics", None)
        if metrics is not None:
            metrics.forget_object(dockey)  # reset byte delta for redownloads
        self.manager.delete_local_data(dockey)
        self.events.publish({"type": "status", **self.status()})

    # --------------------------------------------------------------- connection

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.manager.client.config = self.connection_config()
            content = self.manager.client.check_basket_content()
            name = content.full_name or "anonymous"
            return True, f"Connected. Welcome, {name} ({len(content.items)} item(s) in basket)."
        except Exception as exc:  # noqa: BLE001 - any failure becomes user-facing text
            return False, str(exc)

    # ---------------------------------------------------------------- settings

    def update_settings(self, payload: dict) -> Settings:
        current = self.settings.to_dict()
        current.update({k: v for k, v in payload.items() if k in current})
        # Store normalized: strip stray whitespace from all text fields
        # (autofilled form inputs are the classic source - a leading space in
        # the S-user breaks basic auth with a confusing 401 PWD_WRONG).
        for key, value in current.items():
            if isinstance(value, str):
                current[key] = value.strip()
        self.settings = Settings.from_dict(current)
        self.settings.max_concurrent_objects = max(1, self.settings.max_concurrent_objects)
        self.settings.watch_interval = max(1, self.settings.watch_interval)
        save_settings(self.data_dir, self.settings)
        self.reload_engine()
        self._apply_log_level()
        if self.watcher is not None:
            self.watcher.apply()
        return self.settings

    def settings_view(self) -> dict:
        view = self.settings.to_dict()
        view["configured"] = self.is_configured()
        view["complete"] = self.settings.is_complete()
        return view

    # ------------------------------------------------------------------ protocol

    def object_protocol(self, dockey: str) -> list[dict]:
        return [
            {"ts": p.ts, "type": p.type, "message": p.message} for p in self.store.protocol(dockey)
        ]

    def object_detail(self, dockey: str) -> dict:
        """Everything known about one object: basket metadata, local files
        and the description text (if the description file still exists)."""
        record = self.store.get_object(dockey)
        if record is None:
            raise KeyError(dockey)

        status_texts = {
            0: "not started",
            1: "in progress",
            2: "completed",
            3: "completed",
        }
        files = []
        for comp in self.store.components(dockey):
            files.append(
                {
                    "file_name": comp.file_name,
                    "local_file": comp.local_file_name,
                    "bytes_committed": comp.bytes_committed,
                    "status": status_texts.get(comp.file_status, "unknown"),
                }
            )

        description = ""
        if record.desc_file:
            desc_path = Path(record.desc_file)
            try:
                store_root = Path(self.settings.download_store).resolve()
                if desc_path.resolve().is_relative_to(store_root) and desc_path.is_file():
                    description = desc_path.read_text(encoding="utf-8", errors="replace")[:20000]
            except OSError:
                description = ""

        return {
            "dockey": record.dockey,
            "text": record.text,
            "doc_type": record.doctype,
            "size_kb": record.size_kb,
            "global_status": record.global_status,
            "basket_status": record.basket_status,
            "local_status": record.local_status,
            "files": files,
            "desc_file": record.desc_file,
            "description": description,
        }

    def history(self) -> list[dict]:
        return [
            {
                "dockey": h.dockey,
                "text": h.text,
                "doc_type": h.doctype,
                "version": h.version,
                "date": h.date,
            }
            for h in self.store.history()
        ]

    # ------------------------------------------------------------- file serving

    def object_files(self, dockey: str) -> list[dict]:
        """Completed component files of an object (metadata only)."""
        files = []
        for comp in self.store.components(dockey):
            if comp.file_status != FileStatus.COMPLETED or not comp.file_name:
                continue
            files.append(
                {"file_name": comp.file_name, "size": self._local_size(comp.local_file_name)}
            )
        return files

    def resolve_local_file(self, dockey: str, file_name: str) -> Path | None:
        """Resolve a completed component file by its portal file name.

        Confinement is double-checked: the name must belong to one of the
        object's completed components, and the recorded on-disk path must
        still resolve inside the download store.
        """
        root = self.store_root()
        for comp in self.store.components(dockey):
            if comp.file_name != file_name:
                continue
            if comp.file_status != FileStatus.COMPLETED or not comp.local_file_name:
                return None
            try:
                path = Path(comp.local_file_name).resolve()
            except OSError:
                return None
            if path.is_relative_to(root) and path.is_file():
                return path
            return None
        return None

    def store_root(self) -> Path:
        return Path(self.settings.download_store or self.download_dir).resolve()

    def _local_size(self, local_file_name: str | None) -> int:
        if not local_file_name:
            return 0
        try:
            return Path(local_file_name).stat().st_size
        except OSError:
            return 0
