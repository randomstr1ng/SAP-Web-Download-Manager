"""MCP server: full remote control for AI agents.

FastMCP mounted into the FastAPI app at /mcp; tools map 1:1 to the service
layer. Auth: when DLM_MCP_TOKEN is set, requests must carry
``Authorization: Bearer <token>`` (checked by the FastAPI middleware in app.py).

Adding basket items is not part of the client protocol (use the Software Download Center web UI).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from sapdlm.api.service import Service


def create_mcp_server(service_getter) -> FastMCP:
    """Build the MCP tool server. ``service_getter`` returns the live Service."""

    def svc() -> Service:
        return service_getter()

    mcp: FastMCP[Any] = FastMCP(
        name="sapdlm",
        instructions=(
            "SAP Download Manager remote control. Use list_basket_items to see "
            "the SAP Support Portal download basket, refresh_basket after "
            "changes on the portal, start_download to fetch objects to the "
            "server's download store. Long downloads: start_download returns "
            "immediately; poll with get_status / get_download_protocol."
        ),
    )

    # ------------------------------------------------------------------ status

    @mcp.tool
    def get_status() -> dict:
        """Connection/busy state, version, configured flag, active downloads."""
        return svc().status()

    # ------------------------------------------------------------------- basket

    @mcp.tool
    def list_basket_items() -> dict:
        """Current download basket with per-object status texts."""
        return svc().basket_view()

    @mcp.tool
    def refresh_basket() -> dict:
        """Fetch the basket content from the SAP Support Portal (blocking)."""
        return svc().refresh_basket()

    @mcp.tool
    def remove_from_basket(dockey: str) -> dict:
        """Remove an object from the basket: deletes local data and issues
        OBJDEL on the portal."""
        svc().remove_from_basket(dockey)
        return {"removed": dockey}

    # ---------------------------------------------------------------- downloads

    @mcp.tool
    def start_download(keys: list[str] | None = None,
                       mode: str = "selected") -> dict:
        """Start downloading objects (by keys) or the whole basket (mode=all).
        Returns a task id; monitor via get_status / get_download_protocol."""
        service = svc()
        resolved = service.download_keys(mode, keys)
        if not resolved:
            return {"error": "No objects to download"}
        task_id = service.tasks.create("download", detail=", ".join(resolved))
        _spawn(service, task_id, resolved)
        return {"task_id": task_id, "keys": resolved}

    @mcp.tool
    def pause_download(dockey: str) -> dict:
        """Pause a running object download (resumable)."""
        return {"dockey": dockey, "paused": svc().pause_download(dockey)}

    @mcp.tool
    def resume_download(dockey: str) -> dict:
        """Resume a paused object download (engine resumes from committed
        bytes). Returns the new task id."""
        service = svc()
        if service.manager.is_downloading(dockey):
            return {"dockey": dockey, "resumed": False,
                    "already_downloading": True}
        task_id = service.tasks.create("download", detail=dockey)
        _spawn(service, task_id, [dockey])
        return {"dockey": dockey, "resumed": True, "task_id": task_id}

    @mcp.tool
    def cancel_download(dockey: str) -> dict:
        """Stop the transfer for an object; partial data stays for later
        resume or explicit deletion."""
        return {"dockey": dockey, "cancelled": svc().pause_download(dockey)}

    @mcp.tool
    def delete_local_data(dockey: str) -> dict:
        """Delete partially/completely downloaded data for an object."""
        svc().delete_local_data(dockey)
        return {"deleted": dockey}

    # --------------------------------------------------------------- inspection

    @mcp.tool
    def get_download_protocol(dockey: str) -> dict:
        """Timestamped protocol messages for one object (info + errors)."""
        return {"dockey": dockey, "entries": svc().object_protocol(dockey)}

    @mcp.tool
    def get_download_history() -> dict:
        """Recently completed downloads (latest 250)."""
        return {"entries": svc().history()}

    @mcp.tool
    def get_task(task_id: str) -> dict:
        """State of a background task (running/done/error)."""
        task = svc().tasks.get(task_id)
        return task or {"error": "Unknown task"}

    # ---------------------------------------------------------------- schedule

    @mcp.tool
    def schedule_download(schedule_at: str, mode: str = "all") -> dict:
        """Schedule a one-shot download (ISO datetime, must be in the future).
        Pass an empty string to clear the schedule."""
        from sapdlm.api.scheduler import parse_iso

        service = svc()
        if not schedule_at.strip():
            service.scheduler.cancel()
            return service.scheduler.status()
        when = parse_iso(schedule_at.strip())
        service.scheduler.schedule(when, mode)
        return service.scheduler.status()

    # ------------------------------------------------------------------ config

    @mcp.tool
    def get_config() -> dict:
        """Non-secret settings and completeness/configured flags."""
        return svc().settings_view()

    @mcp.tool
    def update_config(settings: dict) -> dict:
        """Update non-secret settings (same keys as get_config returns,
        without 'configured'/'complete'). Secrets are never set via MCP."""
        return svc().update_settings(settings).to_dict()

    # ------------------------------------------------------------------ helper

    def _spawn(service: Service, task_id: str, keys: list[str]) -> None:
        import asyncio

        loop = service.events._loop  # noqa: SLF001 - same-package wiring
        if loop is None or loop.is_closed():
            service.tasks.finish(task_id, "no running event loop")
            return

        async def run() -> None:
            try:
                await asyncio.to_thread(service.run_download, keys)
                service.tasks.finish(task_id)
            except Exception as exc:  # noqa: BLE001
                service.tasks.finish(task_id, str(exc))

        loop.call_soon_threadsafe(lambda: loop.create_task(run()))

    return mcp
