# How It Works

This document explains what the SAP Web Download Manager does, how it is built,
and how to operate it.

## What it is

A feature-compatible web-based reimplementation of the official **SAP Download
Manager** (a Java/Swing desktop application). The official client downloads the
contents of your SAP Support Portal *download basket* to your workstation; this
project does the same, but runs as a server — you operate it through a browser
from anywhere, and the files land on the server's disk instead of your laptop.

Feature set (matching the original):

- Download basket view (object / size / date / status), refresh from the portal
- Manual download of selected objects, automatic download of the whole basket
- Resumable downloads (HTTP Range, `.part` files, crash-safe checkpoints)
- Pause / resume / cancel per object; version-mismatch resets
- Per-object protocol log; download history (250 entries)
- Delete partially downloaded data; remove objects from the basket (OBJDEL)
- Scheduled one-shot download (survives restarts) and auto-download at startup
- Basic auth (S-User/password) or X.509 client certificate; optional proxy
- Description files incl. `BINARY_PATCHES` conversion; per-object sub-directories
- Upgrade notice banner; the original's error texts for 401/403/404/503/…

Additions that go beyond the original:

- Responsive OpenUI5 web UI with light/dark theme (Horizon), running on a
  headless server, served over plain HTTP for internal networks
- WebSocket live progress (percentage in the row and on the bar)
- REST API (OpenAPI docs included) and an MCP server for AI-agent remote control
- Credential store with masked API round-trip; Docker delivery

**Not supported:** adding items to the basket from this tool — use the
[Software Download Center](https://support.sap.com/swdc) web UI to fill your
basket. Column-sort buttons in the basket table (search/filter work) and
canceling a running basket refresh are minor known gaps.

## Architecture

```
┌───────────── Browser ─────────────┐
│  OpenUI5 app (web/, vendored ui5/) │
│   REST /api/*  ·  WebSocket /ws    │
└──────────────┬────────────────────┘
               │ plain HTTP (internal)
┌──────────────▼─────────────────────────────────────┐
│ FastAPI app (sapdlm/)                              │
│  routes/  REST endpoints + WS event bus           │
│  mcp.py  MCP tool server at /mcp/mcp             │
│  service.py  wiring: settings, credentials, tasks │
│  scheduler.py  one-shot scheduled downloads       │
│  watcher.py  periodic refresh + auto-download     │
│  metrics.py  Prometheus /metrics endpoint        │
├───────────────────────────────────────────────────┤
│ Download engine                                   │
│  manager.py  per-object state machine             │
│  component.py  resumable single-file downloader   │
│  description.py  description writer               │
│  state.py  SQLite persistence (objects,           │
│            components, protocol, history)         │
├───────────────────────────────────────────────────┤
│ Protocol client                                   │
│  download_client.py  basket operations            │
│  endpoints.py  URL builders (2 endpoint families) │
│  parsers.py  response parsers (JSON + key=value)  │
│  connection.py  httpx layer: basic/X.509 auth,   │
│            proxy, enforced https, retry semantics │
└──────────────────┬────────────────────────────────┘
                   │ HTTPS (enforced)
        SAP Support Portal / SWDC  +  SAP/Akamai file servers
```

The wire protocol is documented in detail in
[PROTOCOL.md](PROTOCOL.md) — the same protocol the official Java client uses
(`/odata/svt/swdcuisrv/DownloadContentSet?_MODE=...` on the Support Portal,
or the legacy Service Marketplace key=value format, selected automatically
from the configured URL).

### Object download flow (per object)

1. `OBJECT_VERSION` — fetch the object's version and status from the portal
2. Validate local state: version mismatch resets the object; over-long partial
   files are truncated to the trusted committed length; missing files restart
3. `DOWNLOAD_START2` — get component URLs (validated against the SAP/Akamai
   host allowlist) and download tokens
4. Download components sequentially: token cookie, HTTP Range resume (206 →
   append to `.part`, 200 → restart from zero), checkpoints every ~500 KB,
   rename on completion (collision suffixes `.1`, `.2`, …)
5. Optionally write the description file (incl. `BINARY_PATCHES` conversion)
6. `DOWNLOAD_END` — the portal must confirm `BASKETSTATUS=4`, then the object
   is marked complete and a history entry is written

Every step writes timestamped entries to the per-object protocol log
(viewable in the UI), and progress events (bytes, total, speed, component
count) are pushed live over the WebSocket. Objects run one after another by
default; `max_concurrent_objects` runs several object state machines in
parallel (components within an object always stay sequential).

## Configuration

Settings are managed in the web UI (Settings page / first-run wizard) and
persisted to `<data>/config.json`. All text fields are whitespace-normalized
(a stray leading space in the S-user otherwise breaks basic auth).

| Field | Meaning |
|---|---|
| `sap_url` | Portal address, default `https://tech.support.sap.com` |
| `username` | S-User (e.g. `S0012345678`) |
| `use_cert_auth` / `cert_path` | X.509 client certificate (PEM) instead of password |
| `use_proxy`, `proxy_*` | Optional proxy |
| `download_store` | Target directory for downloads |
| `create_subdir` | One sub-directory per object |
| `save_description` | Write the description text file next to the archives |
| `remember_credentials` | Keep credentials on the server (else memory-only) |
| `trace` | Verbose logging |
| `autostart_download` | Download the whole basket at server startup |
| `max_concurrent_objects` | Objects downloaded in parallel (default 1 = one after another, like the original) |
| `component_retries` | Auto-retry attempts per component download (default 3) |
| `watch_mode` / `watch_interval` | Periodically refresh the basket (minutes, default 15) and auto-download items that appeared since the last refresh; cycles are skipped while downloads are running |
| `webhook_url` | POST a JSON notification (`event`, `dockey`, `text`, `timestamp`) after each object completes or fails; the optional secret below is sent as `X-Webhook-Secret` |

### Credentials

Secrets (`sap_password`, `proxy_password`, `cert_key_password`,
`webhook_secret`) are stored in
`<data>/credentials.json` (mode 0600) — the file backend. The API **never**
returns secret values, only a `stored` flag and a mask (`******`). Sending the
mask (or an empty string) keeps the stored value; only a new non-empty value
replaces it, and only the explicit `DELETE /api/credentials/{key}` clears it.
Environment variables (`DLM_SAP_PASSWORD`, `DLM_PROXY_PASSWORD`,
`DLM_PRVT_KEY_PASSWORD`, `DLM_WEBHOOK_SECRET`) override the store for one run
without persisting.

### Environment variables

| Variable | Purpose |
|---|---|
| `DLM_BIND` | Listen address (default `127.0.0.1:8000`; container `0.0.0.0:8000`) |
| `DLM_DATA_DIR` / `DLM_DOWNLOAD_DIR` | State dir / download store (container: `/data/app`, `/data/downloads`) |
| `DLM_ADMIN_USER` / `DLM_ADMIN_PASSWORD` | Optional HTTP Basic auth for the REST API |
| `DLM_MCP_TOKEN` | Optional Bearer token for the MCP endpoint |
| `DLM_WEB_DIR` / `DLM_UI5_DIR` | Override static UI paths |
| `DLM_LOG_LEVEL` | uvicorn log level |

## API surface

REST under `/api` (interactive docs at `/docs`):

- `GET /api/state`, `GET /api/status`, `GET /api/health`
- `GET /api/basket`, `POST /api/basket/refresh`, `POST /api/basket/refresh/cancel`, `DELETE /api/basket/{dockey}`
- `POST /api/downloads/start` (keys or `mode=all`), `POST .../{dockey}/pause|resume|cancel`, `DELETE .../{dockey}/local`
- `GET /api/objects/{dockey}/protocol`, `GET /api/history`
- `GET /api/objects/{dockey}/files` — completed local files of an object;
  `GET /api/objects/{dockey}/files/{name}` streams one from the download
  store (attachment, HTTP Range/`206` support, resume-friendly); only
  files recorded for that object and still confined to the store are served
  — the detail dialog links each completed file
- `GET/PUT /api/config`, `POST /api/config/test`, `GET/PUT /api/credentials`
- `GET/POST/DELETE /api/schedule`
- `GET /api/tasks/{id}` — background tasks return `202` + task id
- `GET /metrics` — Prometheus text format (admin-protected like the API)

WebSocket `/ws` pushes events: `hello`, `status`, `progress`
(bytes/total/speed/component), `error`. UI assets are served with
`Cache-Control: no-cache` so rebuilds never mix stale modules.

### MCP server

AI agents can fully drive the application via MCP (streamable HTTP at
`/mcp/mcp`, Bearer token via `DLM_MCP_TOKEN`). Tools: `get_status`,
`list_basket_items`, `refresh_basket`, `remove_from_basket`, `start_download`,
`pause_download`, `resume_download`, `cancel_download`, `delete_local_data`,
`get_download_protocol`, `get_download_history`, `get_task`,
`schedule_download`, `get_config`, `update_config` (non-secret settings only).

## Security model

- The web UI is **internal-only and served over plain HTTP** — by design.
  Access control: optional admin Basic auth; keep the port on an internal
  network or in front of an authenticating proxy.
- Outbound portal traffic is **always HTTPS** (protocol requirement).
- Secrets never leave the API unmasked; the credential file is `0600`.
- Download file names from the portal are sanitized (single path segment,
  no traversal) and confined to the download store. The file-serving
  endpoint additionally resolves only files the engine recorded for the
  requested object (double confinement).
- The container runs as non-root (uid 1000).

## Data layout

```
<data>/config.json        settings (whitespace-normalized)
<data>/credentials.json   secrets (0600, masked API)
<data>/app.db             SQLite: objects, components, protocol, history
<downloads>/              downloaded archives, `.part` partials while running
```

Everything else is fetched at runtime; the container is stateless apart from
the two volumes and safe to replace.

## Docker

Multi-stage `Dockerfile` (builder → slim runtime, non-root, healthcheck on
`/api/health`). The compose file mounts `./data` and `./downloads` as the two
persistent volumes.

**Bind-mount note:** the container runs as uid 1000. If Docker creates
`data/`/`downloads/` itself, they end up root-owned and the app cannot write
its database — create them yourself first (`mkdir -p data downloads`) or fix
ownership with `sudo chown -R 1000:1000 data downloads`. The app preflights
writability at startup and logs a clear error if this happens.

The OpenUI5 runtime (~133 MB) is vendored in `ui5/` (pruned from the official
`openui5-runtime-mobile` package: debug files and unused libraries removed,
plus a generated `sap-ui-version.json`). It ships in the image so the UI works
fully offline / on intranet servers.
