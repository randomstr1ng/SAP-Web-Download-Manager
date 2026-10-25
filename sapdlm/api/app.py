"""FastAPI application: routes, lifespan wiring, auth, static UI."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from sapdlm import DLM_VERSION, __version__
from sapdlm.api.routes import api_router, ws_router
from sapdlm.api.service import Service


def _web_dir() -> Path:
    if os.environ.get("DLM_WEB_DIR"):
        return Path(os.environ["DLM_WEB_DIR"])
    repo_web = Path(__file__).resolve().parent.parent.parent / "web"
    if repo_web.is_dir():
        return repo_web
    return Path("/app/web")  # container layout (Dockerfile stage 2)


def _admin_credentials() -> tuple[str | None, str | None]:
    user = os.environ.get("DLM_ADMIN_USER") or None
    password = os.environ.get("DLM_ADMIN_PASSWORD") or None
    return user, password


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = Path(os.environ.get("DLM_DATA_DIR") or "data")
    download_dir = Path(os.environ.get("DLM_DOWNLOAD_DIR") or "downloads")
    for directory, label in ((data_dir, "DLM_DATA_DIR"), (download_dir, "DLM_DOWNLOAD_DIR")):
        directory.mkdir(parents=True, exist_ok=True)
        if not os.access(directory, os.W_OK | os.X_OK):
            raise RuntimeError(
                f"Directory {directory} is not writable by the current user "
                f"(uid={os.getuid() if hasattr(os, 'getuid') else 'n/a'}). "
                f"Bind-mounted Docker volumes must be owned by uid 1000 (or map "
                f"the container user via compose 'user:'). Create the host "
                f"directories as your own user before starting, e.g. "
                f"'mkdir -p {data_dir} {download_dir}', or fix ownership with "
                f"'sudo chown -R 1000:1000 <dir>'."
            )
    factory = getattr(app.state, "service_factory", None) or Service
    service = factory(data_dir=data_dir, download_dir=download_dir)
    service.events.set_loop(asyncio.get_running_loop())
    app.state.service = service

    # One-shot scheduler: re-arm persisted schedule, honor autostart.
    from sapdlm.api.scheduler import Scheduler

    service.scheduler = Scheduler(service)
    await service.scheduler.load()
    if service.settings.autostart_download and service.is_configured():
        # /a, /x parity: drain the basket automatically at startup.
        async def _autostart() -> None:
            try:
                await asyncio.to_thread(service.refresh_basket)
                keys = await asyncio.to_thread(service.download_keys, "all", None)
                if keys:
                    await asyncio.to_thread(service.run_download, keys)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("sapdlm").warning("autostart failed: %s", exc)

        asyncio.get_running_loop().create_task(_autostart())

    admin_user, admin_password = _admin_credentials()
    app.state.admin_user = admin_user
    app.state.admin_password = admin_password
    yield
    if getattr(service, "scheduler", None):
        service.scheduler.cancel()
    service.store.close()


def create_app(service_factory=None) -> FastAPI:
    # MCP server: created first so its lifespan can be merged into the
    # app's lifespan (Starlette does NOT run lifespans of mounted sub-apps).
    _mcp_holder: dict = {}

    from sapdlm.api.mcp import create_mcp_server

    mcp_server = create_mcp_server(
        lambda: _mcp_holder["app"].state.service)
    mcp_http = mcp_server.http_app(path="/mcp")

    @asynccontextmanager
    async def merged_lifespan(app: FastAPI):
        # FastMCP session manager first, then service wiring.
        async with mcp_http.lifespan(mcp_http):
            async with lifespan(app):
                yield

    app = FastAPI(
        title="SAP Download Manager",
        version=__version__,
        description="Python web rewrite of the SAP Download Manager "
                    "(DLManager.jar 3.2.1)",
        lifespan=merged_lifespan,
    )
    _mcp_holder["app"] = app
    if service_factory is not None:
        app.state.service_factory = service_factory

    # ---------------------------------------------------------------- auth
    def require_admin(
        request: Request,
        authorization: str | None = None,
    ) -> None:
        admin_user = request.app.state.admin_user
        admin_password = request.app.state.admin_password
        if not admin_user or not admin_password:
            return  # auth disabled (internal network decision)
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Basic"},
            )
        import base64

        try:
            decoded = base64.b64decode(header[6:]).decode()
            username, _, password = decoded.partition(":")
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid credentials") from exc
        if not (
            secrets.compare_digest(username, admin_user)
            and secrets.compare_digest(password, admin_password)
        ):
            raise HTTPException(status_code=401, detail="Invalid credentials")

    # ---------------------------------------------------------------- routes
    # WS handles its own auth (token query param); REST requires admin when set.
    app.include_router(ws_router)
    app.include_router(api_router, dependencies=[Depends(require_admin)])

    # MCP server: full remote control; /mcp/mcp is the streamable endpoint.
    mcp_token = os.environ.get("DLM_MCP_TOKEN") or None

    @app.middleware("http")
    async def mcp_token_guard(request: Request, call_next):
        if mcp_token and request.url.path.startswith("/mcp"):
            auth = request.headers.get("Authorization", "")
            if not (auth.startswith("Bearer ") and
                    secrets.compare_digest(auth[7:], mcp_token)):
                return JSONResponse({"detail": "Invalid MCP token"},
                                    status_code=401)
        response = await call_next(request)
        # UI files change between image rebuilds - force revalidation so
        # browsers never mix stale heuristic-cached modules with new ones.
        if request.url.path.startswith(("/ui", "/ui5")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # MCP endpoint (mounted sub-app; token guard middleware above).
    app.mount("/mcp", mcp_http)

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "app_version": __version__,
                "dlm_protocol_version": DLM_VERSION,
            }
        )

    @app.get("/api/health")
    def api_health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/api/state")
    def api_state(request: Request) -> JSONResponse:
        return JSONResponse(request.app.state.service.status())

    # Vendored OpenUI5 runtime (self-hosted, no CDN dependency).
    # NOTE: must be mounted before /ui - Starlette mounts match by prefix.
    ui5 = Path(os.environ.get("DLM_UI5_DIR") or (
        Path(__file__).resolve().parent.parent.parent / "ui5"
    ))
    if ui5.is_dir():
        app.mount("/ui5", StaticFiles(directory=ui5), name="ui5")

    web = _web_dir()
    if web.is_dir():
        app.mount("/ui", StaticFiles(directory=web, html=True), name="ui")
    return app


app = create_app()
