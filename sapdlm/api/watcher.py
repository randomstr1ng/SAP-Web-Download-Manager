"""Watch mode: periodically refresh the basket and auto-download new items.

The watcher keeps a baseline of the items seen in the last basket refresh;
items that appear later are downloaded automatically (once per appearance -
a failed item is not retried every cycle). The baseline is in-memory: after a
restart, the first refresh seeds it again without triggering downloads.

Debounce: a cycle is skipped entirely while downloads are running, so the
watcher never interleaves a basket refresh with an active object download.
"""

from __future__ import annotations

import asyncio
import logging

from sapdlm.api.service import Service

logger = logging.getLogger("sapdlm.watcher")


class Watcher:
    def __init__(self, service: Service):
        self.service = service
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task = None
        self._known: set[str] = set()

    # ------------------------------------------------------------------ control

    async def load(self) -> None:
        """Capture the app loop and start watching if enabled."""
        self._loop = asyncio.get_running_loop()
        if self.service.settings.watch_mode:
            self.start()

    def apply(self) -> None:
        """Align the running loop with the current settings."""
        if self.service.settings.watch_mode:
            if self._task is None or self._task.done():
                self.start()
        else:
            self.stop()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        if self._loop is None:
            return  # not started yet (pre-lifespan)
        logger.info("Watch mode enabled (every %d minute(s)).",
                    self.service.settings.watch_interval)
        # update_settings runs in a worker thread: schedule onto the app loop
        self._task = asyncio.run_coroutine_threadsafe(self._run(), self._loop)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._known.clear()

    def status(self) -> dict:
        return {
            "active": self._task is not None and not self._task.done(),
            "interval_min": self.service.settings.watch_interval,
        }

    # ---------------------------------------------------------------- internals

    def _interval_seconds(self) -> float:
        return max(60.0, self.service.settings.watch_interval * 60)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds())
            service = self.service
            if service.manager.downloading_keys:
                continue  # debounce: never refresh during an active download
            try:
                await asyncio.to_thread(service.refresh_basket)
            except Exception as exc:  # noqa: BLE001 - keep watching
                logger.warning("watch refresh failed: %s", exc)
                continue
            fresh = set(service.basket.items) if service.basket else set()
            new = fresh - self._known
            if not self._known and fresh:
                # First successful refresh: seed the baseline, download nothing.
                self._known = fresh
                continue
            self._known = fresh
            if not new:
                continue
            logger.info("Watch mode: %d new item(s) in the basket.", len(new))
            await asyncio.to_thread(service.run_download, sorted(new))
