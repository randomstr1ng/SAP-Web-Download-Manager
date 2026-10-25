"""Config + credential routes (masked secrets)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from sapdlm.api.routes.basket import svc

router = APIRouter(prefix="/api")

MASK = "******"


class SettingsPayload(BaseModel):
    model_config = {"extra": "forbid"}
    sap_url: str | None = None
    username: str | None = None
    use_cert_auth: bool | None = None
    cert_path: str | None = None
    use_proxy: bool | None = None
    proxy_host: str | None = None
    proxy_port: int | None = None
    proxy_username: str | None = None
    download_store: str | None = None
    create_subdir: bool | None = None
    save_description: bool | None = None
    remember_credentials: bool | None = None
    trace: bool | None = None
    schedule_at: str | None = None
    schedule_mode: str | None = None
    autostart_download: bool | None = None


class CredentialsPayload(BaseModel):
    sap_password: str | None = None
    proxy_password: str | None = None
    cert_key_password: str | None = None


@router.get("/config")
def get_config(request: Request) -> dict:
    return svc(request).settings_view()


@router.put("/config")
def put_config(payload: SettingsPayload, request: Request) -> dict:
    service = svc(request)
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided")
    service.update_settings(updates)
    return service.settings_view()


@router.post("/config/test")
async def test_connection(request: Request) -> dict:
    service = svc(request)
    ok, message = await asyncio.to_thread(service.test_connection)
    return {"ok": ok, "message": message}


@router.get("/credentials")
def get_credentials(request: Request) -> dict:
    return svc(request).credentials.masked()


@router.put("/credentials")
def put_credentials(payload: CredentialsPayload, request: Request) -> dict:
    service = svc(request)
    data = payload.model_dump(exclude_none=True)
    try:
        service.credentials.update_masked(data)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.credentials.masked()


@router.delete("/credentials/{key}", status_code=204)
def delete_credential(key: str, request: Request) -> None:
    """Explicitly clear one stored secret (empty PUTs never clear)."""
    from sapdlm.credentials import SECRET_KEYS

    service = svc(request)
    if key not in SECRET_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown secret '{key}'")
    service.credentials.delete(key)
