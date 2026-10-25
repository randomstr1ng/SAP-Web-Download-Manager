# SAP Download Manager — Python Web Rewrite

Feature-complete rewrite of the SAP Download Manager (`_original/DLManager.jar`,
v3.2.1, Java/Swing) as a headless Python web application with an OpenUI5 web UI,
REST API, WebSocket events, and an MCP server for AI-agent remote control.

## Quickstart

```bash
mkdir -p data downloads        # host dirs must be owned by uid 1000 (see note)
docker compose up --build      # then open http://<host>:8000/ui/
```

The first start opens a wizard asking for the SAP Support Portal URL, S-user,
password and the download store. Everything else works from the web UI.

> **Bind-mount note:** the container runs as non-root uid 1000. If `data/` or
> `downloads/` don't exist when you start the stack, the Docker daemon creates
> them as *root* and the app fails to open its SQLite database. Create them
> yourself first (as shown above) or fix ownership with
> `sudo chown -R 1000:1000 data downloads`. The app preflights writability and
> logs a clear error explaining this if it ever happens.

CLI one-shot (original `/a`, `/x` parity):

```bash
docker compose run --rm sapdlm python -m sapdlm.main --auto
```

## Features (parity with the original)

- Download basket view (object/size/date/status), refresh, multi-select
- Manual download of selected objects, automatic download of the whole basket
- Resumable downloads (HTTP Range, `.part` files, crash-safe checkpoints)
- Pause / resume / cancel per object; version-mismatch resets
- Per-object protocol log, download history (250 entries)
- Delete partially downloaded data; remove object from basket (OBJDEL)
- Scheduled one-shot download (persisted across restarts, missed runs cleared)
- Auto-download at startup, CLI one-shot mode
- Basic auth or X.509 client certificate; optional proxy
- Description files incl. BINARY_PATCHES conversion; per-object sub-directories
- Upgrade notice banner, mapped error texts (401/403/404/503/…)
- Light/dark Fiori theme (Horizon), responsive, i18n

Additions beyond the original: web UI for headless servers, WebSocket progress,
MCP remote control, credential store, Docker delivery.

**Known gaps (backlog):** column-sort buttons in the basket table (search works),
canceling a running basket refresh, `add_to_basket` (postponed; SAP Notes
integration design in `docs/PLAN.md` §7.1, saprfclib path §9.1).

## Architecture

| Layer | Tech |
|---|---|
| Web/API | FastAPI + uvicorn, WebSocket events |
| Protocol client | httpx (portal OData JSON + legacy marketplace key=value) |
| Engine | resumable downloader, SQLite state (`app.db`) |
| UI | OpenUI5 1.148 (self-hosted in `ui5/`), sap.m, Horizon light/dark |
| Remote control | FastMCP at `/mcp/mcp` (Bearer token via `DLM_MCP_TOKEN`) |
| Delivery | Docker (multi-stage, non-root, healthcheck) |

Layout: `sapdlm/` (client/engine/api packages), `web/` (UI), `ui5/` (vendored
runtime), `docs/` (RESEARCH/PLAN/ROADMAP), `_original/` (original jar + decompiled
reference sources), `tests/` (67 tests).

## Configuration

Settings are managed via the web UI (persisted to `data/app/config.json`);
secrets go to the credential store (`data/app/credentials.json`, mode 0600).
Environment variables for bootstrap/secrets:

| Variable | Purpose |
|---|---|
| `DLM_BIND` | Listen address (default `127.0.0.1:8000`, container `0.0.0.0:8000`) |
| `DLM_DATA_DIR` / `DLM_DOWNLOAD_DIR` | State dir / download store (container: `/data/app`, `/data/downloads`) |
| `DLM_ADMIN_USER` / `DLM_ADMIN_PASSWORD` | Optional HTTP Basic auth for the REST UI |
| `DLM_MCP_TOKEN` | Optional Bearer token for `/mcp` |
| `DLM_SAP_PASSWORD` / `DLM_PROXY_PASSWORD` / `DLM_PRVT_KEY_PASSWORD` | One-run secret overrides (not persisted) |
| `DLM_LOG_LEVEL` | uvicorn log level; `settings.trace` toggles verbose app logs |

## Security notes

- The web UI is **internal-only and served over plain HTTP** (project decision).
  Access control: optional admin Basic auth; keep the port on an internal network.
- Outbound traffic to the SAP Support Portal is **always HTTPS** (protocol
  requirement of the original client).
- Secrets are never returned by the API (masked as `******`); the credential
  file is `0600`; MCP config tools cannot set secrets.
- Download paths are sanitized against traversal from portal-provided file names.

## API

REST under `/api` (interactive docs at `/docs`), live events on `/ws`, MCP at
`/mcp/mcp`. MCP tools: `get_status`, `list_basket_items`, `refresh_basket`,
`remove_from_basket`, `start_download`, `pause_download`, `resume_download`,
`cancel_download`, `delete_local_data`, `get_download_protocol`,
`get_download_history`, `get_task`, `schedule_download`, `get_config`,
`update_config`.

## Development

```bash
# uv
uv venv .venv --python 3.12 && uv pip install -p .venv/bin/python -e . pytest respx
.venv/bin/python -m pytest                              # 67 tests
.venv/bin/uvicorn sapdlm.api.app:app --port 8000         # dev server

# or standard venv + pip
python3 -m venv .venv && source .venv/bin/activate
pip install -e . pytest respx && pytest
```

Full walkthrough (venv example, env vars, lint, Docker alternative):
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Documentation

- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — local setup incl. a full **venv
  example** (uv and standard `python -m venv`), running, tests, lint, Docker
- [`docs/RESEARCH.md`](docs/RESEARCH.md) — reverse-engineering report of the original
- [`docs/PLAN.md`](docs/PLAN.md) — architecture, feature mapping, data model
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — milestone plan M0–M8
- `_original/decompiled_java/` — decompiled sources, the protocol reference

## Status

All milestones **M0–M7 done** (M8 backlog: add-to-basket, multi-user, live portal
validation). 67 tests green; Docker image verified (UI, API, MCP handshake,
healthcheck, non-root). The web UI is verified end-to-end in a headless browser
(`scripts/ui_smoke.py`): app renders, first-run wizard opens, basket + settings
pages navigate, and the light/dark theme toggles — with zero console errors.
Screenshots: [`docs/screenshots/`](docs/screenshots/).
