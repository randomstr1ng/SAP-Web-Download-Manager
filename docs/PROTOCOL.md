# The SAP Download Manager Protocol

This document describes how the official SAP Download Manager (Java, v3.2.1)
communicates with the SAP Support Portal / Software Download Center — reverse
engineered from the original client. The SAP Web Download Manager implements
the same protocol; this is the wire-level reference.

## 1. Original architecture (Java client)

The official client consists of two packages:

```
com.sap.fcms.dlclient    headless client library (protocol, download engine, state)
com.sap.fcms.dlmanager   Swing GUI (MainFrame, WelcomeFrame, dialogs) + settings
```

| Class | Role |
|---|---|
| `DownloadClient` | Orchestrator: connection config, busy/connected state, event listeners, basket fetch with retry, delete/remove operations |
| `SAPConnection` / `SAPRequest` / `SAPResponse` | HTTP layer (Apache HttpClient): Basic auth or X.509 client certificate (PKCS#12 + key password), optional proxy, enforced `https`, retry handler (3 attempts) |
| `BasketItems` / `BasketItem` | Basket content model + response parsers (two formats, see §2) |
| `DownloadHandler` | Per-object download state machine: `OBJECT_VERSION` → validate local data → `DOWNLOAD_START2` → component downloads → save description → `DOWNLOAD_END` |
| `LocalStatusItems` / `LocalStatusItem` / `LocalStatusFile` | Persistent resume state (`~/dlclient.localstate`) |
| `DownloadHistory` / `DownloadHistoryEntry` | Download history (`~/dlclient.history`, max 250 entries) |
| `CAppSettings` | Settings (`~/dlgmanager.config`) |
| `WebRFC`, `TabBorm*` | Unfinished vestiges of the legacy Service Marketplace (item resolution by product profile) — not implemented |

## 2. Endpoints

Two endpoint families exist. The original selects between them with a simple
check: whether the configured URL contains `"service"`.

| Operation | Support Portal (default) | Service Marketplace (legacy) |
|---|---|---|
| Read basket | `GET /odata/svt/swdcuisrv/DownloadContentSet?_MODE=BASKET_CONTENT&...&$format=json` | `GET /~form/download_basket?_MODE=BASKET_CONTENT&...` |
| Object info | `...?_MODE=OBJECT_VERSION&OBJID=<key>&_VERSION=3.2.1&$format=json` | `...?_MODE=OBJECT_VERSION&OBJID=<key>&...` |
| Start download | `...?_MODE=DOWNLOAD_START2&OBJID=<key>&...` | `...?_MODE=DOWNLOAD_START2&...` |
| End download | `...?_MODE=DOWNLOAD_END&OBJID=<key>&...` | `...?_MODE=DOWNLOAD_END&...` |
| Remove from basket | `...?_MODE=OBJDEL&OBJID=<key>&$format=json` | `...?_MODE=OBJDEL&OBJID=<key>&` |

`BASKET_CONTENT` additionally sends client telemetry parameters:
`J_VERSION, J_VM_VERSION, J_VENDOR, J_VM_NAME, OS_NAME, OS_ARCH, OS_VERSION, _VERSION`.
The default portal address is `https://tech.support.sap.com`.

## 3. Response formats

**Support Portal (OData JSON):** a stream of `{"Name": ..., "Value": ...}` pairs.

- Basket items: `Value` = `DOCKEY|DOCTYPE|TEXT|SIZE_KB|BASKETSTATUS|YYYYMMDDhhmmss|GLOBALSTATUS` (7 pipe-separated fields)
- Object info: `GLOBALSTATUS`, `INFOHDVERSION`, `ERRORCODE`, `ERRORINFO`
- Download start: `DOCKEY`, `DOWNLOAD<n>` = absolute URL, `TOKEN_<n>` = cookie value, `DESCRIPTION` lines, `DESCRIPTION_CONVERSION` (e.g. `BINARY_PATCHES`)
- Download end: `BASKETSTATUS` must be `4`, otherwise the end status was not saved

**Service Marketplace (legacy):** plain `KEY=value` lines with the same logical
fields (`DOWNLOAD_START2` uses `OBJID` instead of `DOCKEY`).

Error convention: `ERRORCODE` / `ERRORINFO` present → the operation failed.

### Status codes

| `GLOBALSTATUS` | Meaning | `BASKETSTATUS` | Meaning |
|---|---|---|---|
| 0 | OK | 0 | empty |
| 1 | no longer available | 1 | new |
| 2 | not authorized | 2 | update |
| 3 | other error | 3 | download started |
| 5 | system error | 4 | download finished |

## 4. Download engine

- `DOWNLOAD_START2` returns N component URLs (`DOWNLOAD0..n`) plus tokens
  (`TOKEN_<n>`, sent as `Cookie` header on the component request).
- **URL allowlist** (host-suffix match): `.sap-ag.de`, `.sap.com`, `.akadns.net`,
  `.akamai.net`, `.akamaidrm.net`, `.akamaiedge.net`,
  `.deploy.akamaitechnologies.net`, `.edgekey.net`, `.edgesuite.net`.
  URLs failing the allowlist abort the object download.
- **Resume support:** if bytes are committed, a probe GET is made, then a second
  request with `Range: bytes=<offset>-<total>`. `206` → append to `<name>.part`;
  `200` → server does not support resume → delete the partial file and restart.
  On completion the `.part` file is renamed (suffixes `.1`, `.2`, ... on collision).
- State is checkpointed after every ~500 KB, so a kill loses at most ~500 KB.
- Pre-existing partials longer than the committed size are truncated to the
  committed size; shorter ones are deleted. A version mismatch
  (`INFOHDVERSION` vs stored version) resets the whole object.
- Total size is derived from `Content-Range` (`.../total`), preferring it over
  `Content-Length`.
- Basket fetch retries up to 5 times, except on HTTP 401/403 (immediate abort).
  `https` is enforced for all portal requests.

## 5. Description files

Optional "save description" feature: writes the `DESCRIPTION` lines to a text
file named like the first component (extension stripped) or the object key.
`DESCRIPTION_CONVERSION=BINARY_PATCHES` joins all lines with spaces and converts
`rowstart` markers to line separators.

## 6. Settings and local files of the original

| File | Content |
|---|---|
| `dlgmanager.config` | Settings: `SAP_URL`, `USE_CERTIFICATE_AUTH`, `SAP_USERNAME`, `SAP_PASSWORD`, `SAP_CERTIFICATE_PATH`, `SAP_PRVT_KEY_PASSWORD`, `PROXY_HOST/PORT/USERNAME/PASSWORD`, `USE_PROXY`, `DOWNLOAD_STORE`, `CREATE_SUBDIRECTORY`, `SAVE_DESCRIPTION`, `SCHEDULE_*`, `APP_TRACE` |
| `dlclient.localstate` | Resume state per object (components, committed bytes, versions) |
| `dlclient.history` | Up to 250 download-history entries |

The original additionally supports CLI one-shot modes: `/x` (connect +
auto-download), `/a` (automatic download), `/c` (open configuration),
`/p-<password>`, `/r-<password>` (supply passwords without persisting them).

The equivalent concepts in this implementation are documented in
[HOWITWORKS.md](HOWITWORKS.md).
