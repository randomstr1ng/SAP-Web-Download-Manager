# Development Guide

How to run and develop the SAP Download Manager locally (without Docker).
The application is a standard Python 3.12+ project; the only runtime assets are
the vendored OpenUI5 files in `ui5/` (already in the repo).

## Virtual environment (venv) example

**Option A — `uv` (fast, recommended):**

```bash
cd SAP_Download_Manager

# Create the venv (Python 3.12+)
uv venv .venv --python 3.12

# Install the app + dev tooling into it
uv pip install -p .venv/bin/python -e . pytest respx

# Always use the venv's interpreter afterwards
.venv/bin/python -m pytest
```

**Option B — standard library `venv` + pip:**

```bash
cd SAP_Download_Manager

python3 -m venv .venv

# Activate (Linux/macOS) ...
source .venv/bin/activate
# ... or on Windows: .venv\Scripts\activate

pip install --upgrade pip
pip install -e .            # app + runtime dependencies
pip install pytest respx    # test tooling

pytest                      # run the suite (67 tests)
```

Afterwards you can either keep the venv activated or call the binaries directly
(`.venv/bin/pytest`, `.venv/bin/uvicorn`, …).

## Running locally

```bash
# Dev server - UI at http://127.0.0.1:8000/ui/, API docs at /docs
.venv/bin/uvicorn sapdlm.api.app:app --port 8000

# One-shot automatic download of the whole basket, then exit
.venv/bin/python -m sapdlm.main --auto
```

Without `DLM_*` environment variables the app uses `./data` (state, config,
credentials) and `./downloads` (download store) relative to the working
directory — both are git-ignored. Point them elsewhere with:

```bash
export DLM_DATA_DIR=/home/me/dlm-data
export DLM_DOWNLOAD_DIR=/home/me/dlm-downloads
```

Optional variables (full table in the README):

```bash
export DLM_BIND=127.0.0.1:8000          # listen address
export DLM_ADMIN_USER=admin             # optional HTTP Basic auth for /api
export DLM_ADMIN_PASSWORD=...
export DLM_MCP_TOKEN=...                # optional Bearer token for /mcp
export DLM_SAP_PASSWORD=...             # one-run secret override (not persisted)
export DLM_WEBHOOK_SECRET=...           # webhook secret override (not persisted)
```

## Tests

```bash
.venv/bin/python -m pytest              # everything
.venv/bin/python -m pytest tests/test_engine.py -q   # one module
```

The tests cover the protocol parsers (both endpoint styles), the resumable
download engine, the REST API (via a mocked portal), scheduling/autostart, the
MCP tools, and security hardening. No SAP connectivity is required.

## UI smoke test (headless browser)

`scripts/ui_smoke.py` drives the real web UI in headless Chromium against a
stub portal — no SAP account needed. It fails on any uncaught page error and
on a broken theme toggle; screenshots land in `/tmp` (or `$UI_SMOKE_SHOT_DIR`).

```bash
.venv/bin/pip install playwright && playwright install chromium
.venv/bin/python scripts/mock_ui_server.py --port 8010 &
UI_SMOKE_BASE_URL=http://127.0.0.1:8010/ui/ .venv/bin/python scripts/ui_smoke.py
```

Set `CHROMIUM_PATH=/usr/bin/chromium-browser` to use a system Chromium
instead of the Playwright-managed one. CI runs the same flow as a dedicated
job on every push/pull request.

## Lint / formatting

The project targets `ruff` (config in `pyproject.toml`):

```bash
uv pip install -p .venv/bin/python ruff
.venv/bin/ruff check sapdlm tests
.venv/bin/ruff format --check sapdlm tests
```

## Docker (alternative to venv)

```bash
mkdir -p data downloads                 # must be owned by uid 1000 (bind mounts)
docker compose up --build                # production-style container
docker compose run --rm sapdlm python -m sapdlm.main --auto
```

If Docker creates `data/` or `downloads/` itself, they end up root-owned and the
app cannot write its SQLite DB — the startup preflight then fails with a clear
message. Fix: `sudo chown -R 1000:1000 data downloads`.

## Web UI development notes

- Views/controllers live in `web/` (OpenUI5 XML views + controllers, i18n in
  `web/i18n/i18n.properties`); served at `/ui` by the same uvicorn process.
- Localization: `i18n.properties` is the English base bundle, `i18n_de.properties`
  the German one. UI5 picks the browser locale automatically; an explicit choice
  in Settings is stored in the browser (`localStorage`) and applied before UI5
  loads. Any new key must be added to ALL bundles (base + `i18n_en`,
  `i18n_en_US`, `i18n_de`).
- The OpenUI5 runtime is vendored in `ui5/resources/` — do not commit changes to
  it; it was pruned (debug files, unused libraries) from the official
  `openui5-runtime-mobile` package.
- Browser dev tools work fine against the plain-HTTP dev server; there is no
  build step for the UI (no npm/node needed).
