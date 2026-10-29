"""Download routes: start (selected/all), pause, resume, cancel, local cleanup,
protocol view, history."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.responses import FileResponse, Response

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


@router.get("/objects/{dockey}/files")
def object_files(dockey: str, request: Request) -> dict:
    """Completed local files of an object (for fetching via the browser)."""
    service = svc(request)
    _resolve_key(service, dockey)
    return {"dockey": dockey, "files": service.object_files(dockey)}


@router.get("/objects/{dockey}/files/{file_name}")
def object_file(dockey: str, file_name: str, request: Request) -> Response:
    """Stream a completed component file from the download store.

    Supports HTTP Range requests (206 + Content-Range) so browsers can
    resume interrupted transfers."""
    service = svc(request)
    _resolve_key(service, dockey)
    path = service.resolve_local_file(dockey, file_name)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail="The file does not exist (or is not part of a completed "
                   "download on this server).",
        )
    size = path.stat().st_size
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}",
    }
    range_header = request.headers.get("range")
    if range_header:
        start, end = _parse_range(range_header, size)
        if start is None:
            raise HTTPException(
                status_code=416,
                detail=f"Requested range is not satisfiable (file size: {size}).",
                headers={"Content-Range": f"bytes */{size}"},
            )
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(
            _slice(path, start, end - start + 1),
            status_code=206, media_type="application/octet-stream",
            headers=headers,
        )
    return FileResponse(path, media_type="application/octet-stream",
                        headers=headers)


def _parse_range(header: str, size: int) -> tuple[int | None, int]:
    """Parse 'bytes=a-b' / 'bytes=a-' / 'bytes=-n'. Returns (None, size) on
    an unsatisfiable range; malformed headers fall back to a full response
    by raising no error - the caller treats them as no-range."""
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        return 0, size - 1  # malformed: serve the whole file
    if m.group(1):  # "a-b" or "a-"
        start = min(int(m.group(1)), size)
        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    else:  # suffix "-n": the last n bytes
        n = int(m.group(2))
        if n == 0:
            return None, size
        start = max(0, size - n)
        end = size - 1
    if start > end or start >= size:
        return None, size
    return start, end


def _slice(path, start: int, length: int, chunk: int = 1_048_576):
    """Generator: stream `length` bytes from `path` starting at `start`."""
    with open(path, "rb") as f:
        f.seek(start)
        while length > 0:
            data = f.read(min(chunk, length))
            if not data:
                break
            length -= len(data)
            yield data


@router.get("/history")
def get_history(request: Request) -> dict:
    return {"entries": svc(request).history()}
