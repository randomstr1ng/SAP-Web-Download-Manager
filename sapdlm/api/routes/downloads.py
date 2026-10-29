"""Download routes: start (selected/all), pause, resume, cancel, local cleanup,
protocol view, history."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from sapdlm.api.routes.basket import _require_configured, svc
from sapdlm.api.service import Service

router = APIRouter(prefix="/api")


class StartRequest(BaseModel):
    keys: list[str] | None = None
    mode: str = "selected"  # "selected" | "all"


@router.post("/downloads/start", status_code=202)
async def start_downloads(body: StartRequest, request: Request,
                          background: BackgroundTasks) -> dict:
    service = svc(request)
    _require_configured(service)
    keys = service.download_keys(body.mode, body.keys)
    if not keys:
        raise HTTPException(status_code=400, detail="No objects to download")
    task_id = service.tasks.create("download", detail=", ".join(keys))

    async def run() -> None:
        try:
            await asyncio.to_thread(service.run_download, keys)
            service.tasks.finish(task_id)
        except Exception as exc:  # noqa: BLE001
            service.tasks.finish(task_id, str(exc))
            service.events.publish({"type": "error", "message": str(exc)})

    background.add_task(run)
    return {"task_id": task_id, "state": "running", "keys": keys}


def _resolve_key(service: Service, dockey: str) -> None:
    if service.store.get_object(dockey) is None and (
        service.basket is None or dockey not in service.basket.items
    ):
        raise HTTPException(
            status_code=404,
            detail=f"There is no item with the object id '{dockey}' in your "
                   "download basket.",
        )


@router.post("/downloads/{dockey}/pause", status_code=202)
async def pause_download(dockey: str, request: Request) -> dict:
    service = svc(request)
    _resolve_key(service, dockey)
    stopped = service.pause_download(dockey)
    return {"dockey": dockey, "paused": stopped,
            "downloading": service.manager.is_downloading(dockey)}


@router.post("/downloads/{dockey}/resume", status_code=202)
async def resume_download(dockey: str, request: Request,
                          background: BackgroundTasks) -> dict:
    service = svc(request)
    _resolve_key(service, dockey)
    if service.manager.is_downloading(dockey):
        return {"dockey": dockey, "resumed": False, "already_downloading": True}
    task_id = service.tasks.create("download", detail=dockey)

    async def run() -> None:
        try:
            await asyncio.to_thread(service.run_download, [dockey])
            service.tasks.finish(task_id)
        except Exception as exc:  # noqa: BLE001
            service.tasks.finish(task_id, str(exc))
            service.events.publish({"type": "error", "message": str(exc)})

    background.add_task(run)
    return {"dockey": dockey, "resumed": True, "task_id": task_id}


@router.post("/downloads/{dockey}/cancel", status_code=202)
async def cancel_download(dockey: str, request: Request) -> dict:
    """Cancel = stop the transfer; partial data stays on disk so the object can
    be resumed later or deleted explicitly."""
    service = svc(request)
    _resolve_key(service, dockey)
    stopped = service.pause_download(dockey)
    return {"dockey": dockey, "cancelled": stopped}


@router.delete("/downloads/{dockey}/local", status_code=204)
async def delete_local_data(dockey: str, request: Request) -> None:
    service = svc(request)
    _resolve_key(service, dockey)
    if service.manager.is_downloading(dockey):
        raise HTTPException(
            status_code=409,
            detail="The download client is busy with this object. Pause it first.",
        )
    await asyncio.to_thread(service.delete_local_data, dockey)


@router.get("/objects/{dockey}")
def object_detail(dockey: str, request: Request) -> dict:
    """Full object detail: basket metadata, local files and description."""
    try:
        return svc(request).object_detail(dockey)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"There is no item with the object id '{dockey}'.",
        ) from exc


@router.get("/objects/{dockey}/protocol")
def object_protocol(dockey: str, request: Request) -> dict:
    service = svc(request)
    return {"dockey": dockey, "entries": service.object_protocol(dockey)}


@router.get("/history")
def get_history(request: Request) -> dict:
    return {"entries": svc(request).history()}
