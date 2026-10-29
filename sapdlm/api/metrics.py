"""Prometheus text-format metrics (no client dependency).

Counters and gauges for downloads, failures, transferred bytes and the
basket size. Exposed at GET /metrics.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from sapdlm.api.service import Service

_METRIC_HELP = {
    "sapdlm_downloads_total": (
        "Counter", "Number of successfully completed object downloads."),
    "sapdlm_download_failures_total": (
        "Counter", "Number of failed object downloads."),
    "sapdlm_bytes_downloaded_total": (
        "Counter", "Total bytes downloaded from the SAP file servers."),
    "sapdlm_active_downloads": (
        "Gauge", "Number of objects currently downloading."),
    "sapdlm_basket_items": (
        "Gauge", "Number of objects in the download basket."),
}


class Metrics:
    """Thread-safe metric registry for the service."""

    def __init__(self, service: Service):
        self._service = service
        self._lock = threading.Lock()
        self._counters = {
            "sapdlm_downloads_total": 0,
            "sapdlm_download_failures_total": 0,
            "sapdlm_bytes_downloaded_total": 0,
        }
        self._bytes_per_object: dict[str, int] = {}  # last committed bytes

    # ---------------------------------------------------------------- hooks

    def track_object_finished(self, dockey: str, success: bool) -> None:
        with self._lock:
            key = ("sapdlm_downloads_total" if success
                   else "sapdlm_download_failures_total")
            self._counters[key] += 1
            # Progress events are throttled to ~1/s; take the final committed
            # bytes from the state store so the counter is exact.
            final = sum(
                c.bytes_committed
                for c in self._service.store.components(dockey)
            )
            previous = self._bytes_per_object.get(dockey, 0)
            if final > previous:
                self._counters["sapdlm_bytes_downloaded_total"] += final - previous
                self._bytes_per_object[dockey] = final

    def track_progress(self, dockey: str, bytes_committed: int) -> None:
        """Accumulate the committed-byte delta per object."""
        with self._lock:
            previous = self._bytes_per_object.get(dockey, 0)
            if bytes_committed > previous:
                self._counters["sapdlm_bytes_downloaded_total"] += (
                    bytes_committed - previous)
                self._bytes_per_object[dockey] = bytes_committed

    def forget_object(self, dockey: str) -> None:
        """Local data deleted -> the byte delta would be wrong on redownload."""
        with self._lock:
            self._bytes_per_object.pop(dockey, None)

    # ---------------------------------------------------------------- render

    def render(self) -> str:
        with self._lock:
            counters = dict(self._counters)
        active = len(self._service.manager.downloading_keys)
        basket_items = (len(self._service.basket.items)
                        if self._service.basket else 0)
        lines = []
        for name, (kind, help_text) in _METRIC_HELP.items():
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind.lower()}")
            if name in counters:
                lines.append(f"{name} {counters[name]}")
            elif name == "sapdlm_active_downloads":
                lines.append(f"{name} {active}")
            elif name == "sapdlm_basket_items":
                lines.append(f"{name} {basket_items}")
        return "\n".join(lines) + "\n"


def install_metrics(service: Service) -> Metrics:
    """Attach a Metrics instance to the service and hook it into the engine.

    Called once during app startup; also re-attached after settings reload
    (Service.reload_engine keeps the same manager, so the hooks survive).
    """
    if getattr(service, "metrics", None) is not None:
        return service.metrics  # already installed

    metrics = Metrics(service)
    service.metrics = metrics

    original_run = service.run_download

    def run_download_with_metrics(keys: list[str],
                                  progress: Callable | None = None) -> None:
        def progress_hook(update):
            metrics.track_progress(update.dockey, update.bytes_committed)
            if progress is not None:
                progress(update)
            else:
                service.events.publish({"type": "progress", **vars(update)})

        for dockey in keys:
            service.events.publish({"type": "status", **service.status(),
                                    "action": f"Downloading {dockey}"})
            try:
                service.manager.download_object(dockey, progress=progress_hook)
                metrics.track_object_finished(dockey, success=True)
            except Exception as exc:  # engine aborts on this object
                metrics.track_object_finished(dockey, success=False)
                from sapdlm.api.service import logging
                logging.getLogger("sapdlm").warning(
                    "download of %s failed: %s", dockey, exc)
            finally:
                service.events.publish({"type": "status", **service.status()})

    service.run_download = run_download_with_metrics  # type: ignore[method-assign]

    # byte deltas must reset when local data is deleted
    original_delete = service.delete_local_data

    def delete_local_data_with_metrics(dockey: str) -> None:
        metrics.forget_object(dockey)
        original_delete(dockey)

    service.delete_local_data = delete_local_data_with_metrics  # type: ignore[method-assign]
    return metrics
