"""Tests for the Prometheus /metrics endpoint."""

from __future__ import annotations

import re

from tests.test_api import configure, make_client, wait_task


def _parse(resp_text: str) -> dict[str, float]:
    return {
        m.group(1): float(m.group(2))
        for m in re.finditer(r"^(\w+) ([0-9.]+)$", resp_text, re.MULTILINE)
    }


def test_metrics_endpoint(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch=monkeypatch) as client:
        # unconfigured but reachable
        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "text/plain" in metrics.headers["content-type"]
        values = _parse(metrics.text)
        assert values["sapdlm_downloads_total"] == 0
        assert values["sapdlm_active_downloads"] == 0
        assert values["sapdlm_basket_items"] == 0

        configure(client)
        wait_task(client, client.post("/api/basket/refresh").json()["task_id"])
        wait_task(client, client.post("/api/downloads/start",
                                     json={"keys": ["K1"]}).json()["task_id"])

        values = _parse(client.get("/metrics").text)
        assert values["sapdlm_downloads_total"] == 1
        assert values["sapdlm_download_failures_total"] == 0
        assert values["sapdlm_bytes_downloaded_total"] == 100_000
        assert values["sapdlm_basket_items"] == 1

        # delete local data -> byte tracking resets for a future redownload
        client.delete("/api/downloads/K1/local")
        values = _parse(client.get("/metrics").text)
        # counters are monotonic; the reset only affects future deltas
        assert values["sapdlm_downloads_total"] == 1
