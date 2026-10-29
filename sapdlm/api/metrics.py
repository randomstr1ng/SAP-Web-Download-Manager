"""Prometheus text-format metrics (no client dependency).

Counters and gauges for downloads, failures, transferred bytes and the
basket size. Exposed at GET /metrics. The service calls the tracking
methods from its download loop (sapdlm/api/service.py).
"""

from __future__ import annotations

import threading

from sapdlm.api.service import Service

_METRIC_HELP = {
    "sapdlm_downloads_total": ("Counter", "Number of successfully completed object downloads."),
    "sapdlm_download_failures_total": ("Counter", "Number of failed object downloads."),
    "sapdlm_bytes_downloaded_total": (
        "Counter",
        "Total bytes downloaded from the SAP file servers.",
    ),
    "sapdlm_active_downloads": ("Gauge", "Number of objects currently downloading."),
    "sapdlm_basket_items": ("Gauge", "Number of objects in the download basket."),
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
            key = "sapdlm_downloads_total" if success else "sapdlm_download_failures_total"
            self._counters[key] += 1
            # Progress events are throttled to ~1/s; take the final committed
            # bytes from the state store so the counter is exact.
            final = sum(c.bytes_committed for c in self._service.store.components(dockey))
            previous = self._bytes_per_object.get(dockey, 0)
            if final > previous:
                self._counters["sapdlm_bytes_downloaded_total"] += final - previous
                self._bytes_per_object[dockey] = final

    def track_progress(self, dockey: str, bytes_committed: int) -> None:
        """Accumulate the committed-byte delta per object."""
        with self._lock:
            previous = self._bytes_per_object.get(dockey, 0)
            if bytes_committed > previous:
                self._counters["sapdlm_bytes_downloaded_total"] += bytes_committed - previous
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
        basket_items = len(self._service.basket.items) if self._service.basket else 0
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
    """Create the Metrics registry and attach it to the service. Called once
    during app startup; Service.run_download/delete_local_data call the
    tracking methods."""
    if getattr(service, "metrics", None) is not None:
        return service.metrics
    service.metrics = Metrics(service)
    return service.metrics
