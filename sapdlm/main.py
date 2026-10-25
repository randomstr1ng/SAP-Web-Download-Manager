"""Entrypoint: web server (default) or one-shot automatic download (--auto)."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path


def _service_from_env() -> "Service":  # pragma: no cover - thin env wiring
    from sapdlm.api.service import Service

    data_dir = Path(os.environ.get("DLM_DATA_DIR") or "data")
    download_dir = Path(os.environ.get("DLM_DOWNLOAD_DIR") or "downloads")
    return Service(data_dir=data_dir, download_dir=download_dir)


def run_auto() -> int:
    """/a, /x parity: fetch the basket and download everything, then exit."""
    service = _service_from_env()
    try:
        if not service.is_configured():
            print("Configuration incomplete. Configure via the web UI first.")
            return 2
        print("Reading download basket ...")
        service.refresh_basket()
        keys = service.download_keys("all", None)
        if not keys:
            print("The download basket is empty.")
            return 0
        print(f"Downloading {len(keys)} object(s) ...")
        service.run_download(keys)
        print("Automatic download finished.")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI reports any failure
        print(f"Automatic download failed: {exc}")
        return 1
    finally:
        service.store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sapdlm",
        description="SAP Download Manager (Python web rewrite)",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="One-shot automatic download of the whole basket, then exit "
             "(original /a, /x parity)",
    )
    parser.parse_args()
    args = parser.parse_args()

    if args.auto:
        raise SystemExit(run_auto())

    import uvicorn

    bind = os.environ.get("DLM_BIND", "127.0.0.1:8000")
    host, _, port = bind.partition(":")
    uvicorn.run(
        "sapdlm.api.app:app",
        host=host or "127.0.0.1",
        port=int(port or 8000),
        log_level=os.environ.get("DLM_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
