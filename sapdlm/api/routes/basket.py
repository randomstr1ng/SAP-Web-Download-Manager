"""Basket routes: view, refresh (background task), remove from basket, tasks."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from sapdlm.api.service import Service

router = APIRouter(prefix="/api")


def svc(request: Request) -> Service:
    return request.app.state.service


def _require_configured(service: Service) -> None:
    if not service.is_configured():
        raise HTTPException(
            status_code=409,
            detail="Configuration incomplete. Provide SAP URL, credentials and "
                   "a download store first.",
        )


@router.get("/basket")
def get_basket(request: Request) -> dict:
    return svc(request).basket_view()


@router.post("/basket/refresh", status_code=202)
async def refresh_basket(request: Request, background: BackgroundTasks) -> dict:
    service = svc(request)
    _require_configured(service)
    task_id = service.tasks.create("refresh")

    async def run() -> None:
        try:
            await asyncio.to_thread(service.refresh_basket)
            service.tasks.finish(task_id)
        except Exception as exc:  # noqa: BLE001 - reported to the client
            service.tasks.finish(task_id, str(exc))
            service.events.publish({"type": "error", "message": str(exc)})

    background.add_task(run)
    return {"task_id": task_id, "state": "running"}


@router.delete("/basket/{dockey}", status_code=204)
async def remove_basket_item(dockey: str, request: Request) -> None:
    service = svc(request)
    _require_configured(service)
    try:
        await asyncio.to_thread(service.remove_from_basket, dockey)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> dict:
    task = svc(request).tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Unknown task")
    return task
