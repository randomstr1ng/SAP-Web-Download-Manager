"""Route package: basket, downloads, config, credentials, WebSocket."""

from fastapi import APIRouter

from sapdlm.api.routes.basket import router as basket_router
from sapdlm.api.routes.config_routes import router as config_router
from sapdlm.api.routes.downloads import router as downloads_router
from sapdlm.api.routes.schedule import router as schedule_router
from sapdlm.api.routes.ws import router as ws_router

api_router = APIRouter()
api_router.include_router(basket_router)
api_router.include_router(downloads_router)
api_router.include_router(config_router)
api_router.include_router(schedule_router)

__all__ = ["api_router", "ws_router"]
