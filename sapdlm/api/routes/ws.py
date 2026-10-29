"""WebSocket endpoint: /ws - progress/status/protocol/error/upgrade events."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws")
async def events(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> None:
    admin = getattr(websocket.app.state, "admin_password", None)
    if admin and (not token or not secrets.compare_digest(token, admin)):
        await websocket.close(code=4001, reason="unauthorized")
        return
    await websocket.accept()
    service = websocket.app.state.service
    service.events.subscribe(websocket)
    try:
        await websocket.send_json({"type": "hello", "status": service.status()})
        while True:
            # Client may send pings; content is ignored.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        service.events.unsubscribe(websocket)


@router.get("/api/ws-info")
def ws_info() -> dict:
    return {"endpoint": "/ws", "auth": "query param 'token' when admin is configured"}
