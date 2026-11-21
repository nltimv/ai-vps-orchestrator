from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aivo import __version__
from aivo.api.deps import get_app, get_session
from aivo.context import AppContext
from aivo.controller.lease import lease_status
from aivo.controller.pause import get_pause

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(app: AppContext = Depends(get_app), s: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    db_ok = True
    paused = False
    try:
        await s.execute(text("SELECT 1"))
        controller = await lease_status(s)
        paused = await get_pause(s) is not None
    except Exception:
        db_ok, controller = False, None
    return {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "db": db_ok,
        "provider": app.provider.name,
        "controller": controller,
        "offers_error": app.offers.error,
        "live_updates": {"connected": app.bus.connected, "error": app.bus.last_error},
        "catalog_models": len(app.catalog.models),
        "provisioning_paused": paused,
    }
