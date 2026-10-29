"""Run the app locally against a stub portal - companion of scripts/ui_smoke.py.

Serves the full web UI (OpenUI5, WebSocket progress, all routes) with the
SAP Support Portal replaced by an in-process httpx.MockTransport (same stub
as the test suite). Used by the Playwright UI smoke test in CI and for local
UI work without touching a real portal account.

    python scripts/mock_ui_server.py [--port 8010] [--data-dir DIR]

The app starts fully configured (stub credentials) so the basket view is
populated right away; the wizard still opens on first visit but can be
dismissed as usual.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from sapdlm.api.app import create_app  # noqa: E402
from sapdlm.api.service import Service  # noqa: E402
from tests.portal_stub import portal_handler  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--download-dir", type=Path, default=None)
    args = parser.parse_args()

    data_dir = args.data_dir or Path(tempfile.mkdtemp(prefix="sapdlm-ui-"))
    download_dir = args.download_dir or data_dir / "downloads"
    data_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)

    # Pre-configure so the smoke run lands on a populated basket page.
    (data_dir / "config.json").write_text(json.dumps({
        "sap_url": "https://launchpad.test",
        "username": "jane",
        "download_store": str(download_dir),
    }), encoding="utf-8")
    (data_dir / "credentials.json").write_text(
        json.dumps({"sap_password": "s3cret"}), encoding="utf-8")

    os.environ["DLM_DATA_DIR"] = str(data_dir)
    os.environ["DLM_DOWNLOAD_DIR"] = str(download_dir)

    def service_factory(data_dir, download_dir, **kw):
        transport = httpx.MockTransport(portal_handler)
        return Service(
            data_dir=data_dir,
            download_dir=download_dir,
            http_client=httpx.Client(transport=transport),
            component_client_factory=lambda: httpx.Client(transport=transport),
        )

    print(f"mock UI server: http://127.0.0.1:{args.port}/ui/ "
          f"(data: {data_dir})", flush=True)
    uvicorn.run(create_app(service_factory=service_factory),
                host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
