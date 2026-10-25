"""Schedule routes (plan 5): GET/POST/DELETE /api/schedule."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from sapdlm.api.routes.basket import _require_configured, svc

router = APIRouter(prefix="/api")


class SchedulePayload(BaseModel):
    schedule_at: str  # ISO datetime; empty string clears the schedule
    schedule_mode: str = "all"


@router.get("/schedule")
def get_schedule(request: Request) -> dict:
    return svc(request).scheduler.status()


@router.post("/schedule")
async def set_schedule(payload: SchedulePayload, request: Request) -> dict:
    service = svc(request)
    if not payload.schedule_at.strip():
        service.scheduler.cancel()
        return service.scheduler.status()
    _require_configured(service)
    from sapdlm.api.scheduler import parse_iso

    try:
        when = parse_iso(payload.schedule_at.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="The scheduled date/time could not be parsed. Use ISO format.",
        ) from exc
    try:
        service.scheduler.schedule(when, payload.schedule_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    service.events.publish({"type": "status", **service.status()})
    return service.scheduler.status()


@router.delete("/schedule", status_code=204)
async def delete_schedule(request: Request) -> None:
    svc(request).scheduler.cancel()
