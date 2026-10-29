"""One-shot scheduled download (MainFrame_Scheduler / ScheduledDownloadThread parity).

The schedule is persisted in the settings (schedule_at/schedule_mode) and re-armed
on startup. A schedule missed while the server was down is cleared and reported
as missed (the original only ran while the GUI was open).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sapdlm.api.service import Service
from sapdlm.config import save_settings

logger = logging.getLogger("sapdlm.scheduler")


def parse_iso(value: str) -> datetime:
    """Parse an ISO datetime; naive values are treated as local time (the UI
    DateTimePicker sends local values)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # attach local tz
    return dt


class Scheduler:
    def __init__(self, service: Service):
        self.service = service
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ arming

    async def load(self) -> None:
        """Re-arm the persisted schedule on startup (restart persistence)."""
        s_at = self.service.settings.schedule_at
        if not s_at:
            return
        try:
            when = parse_iso(s_at)
        except ValueError:
            logger.warning("Invalid persisted schedule_at %r - cleared", s_at)
            self._clear()
            return
        if when <= datetime.now(when.tzinfo):
            logger.info("Scheduled download %s was missed while offline - cleared", when)
            self.service.store.add_protocol(
                "",
                "A scheduled download was missed while the server was offline.",
            )
            self._clear()
            return
        self._arm(when, self.service.settings.schedule_mode or "all")

    def schedule(self, when: datetime, mode: str = "all") -> None:
        if when <= datetime.now(when.tzinfo):
            raise ValueError(
                "The date and time specified for the download scheduler lies "
                "before the current date and time. Check your settings and try again."
            )
        self._arm(when, mode)

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        # Only persist when a schedule actually existed - cancel() also runs
        # during app shutdown and must never rewrite the config file.
        self._clear()

    def status(self) -> dict:
        active = self._task is not None and not self._task.done()
        return {
            "active": active,
            "schedule_at": self.service.settings.schedule_at,
            "mode": self.service.settings.schedule_mode,
        }

    # ---------------------------------------------------------------- internals

    def _arm(self, when: datetime, mode: str) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self.service.settings.schedule_at = when.isoformat()
        self.service.settings.schedule_mode = mode
        save_settings(self.service.data_dir, self.service.settings)
        delay = max((when - datetime.now(when.tzinfo)).total_seconds(), 0.1)
        self._task = asyncio.get_running_loop().create_task(self._run(delay, mode))

    def _clear(self) -> None:
        if not self.service.settings.schedule_at:
            return  # nothing persisted to clear - do not touch the config file
        self.service.settings.schedule_at = ""
        save_settings(self.service.data_dir, self.service.settings)

    async def _run(self, delay: float, mode: str) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._clear()
        service = self.service
        keys = await asyncio.to_thread(service.download_keys, mode, None)
        if not keys:
            logger.info("Scheduled download fired but the basket is empty.")
            return
        logger.info("Scheduled download firing for %d object(s).", len(keys))
        self.service.events.publish(
            {
                "type": "status",
                "action": "Scheduled download started",
            }
        )
        await asyncio.to_thread(service.run_download, keys)
