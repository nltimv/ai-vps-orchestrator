"""Admin: cache (prepared images, mirror, volumes) and housekeeping."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aivo.api.deps import get_app, require_admin
from aivo.cache.manager import CacheManager
from aivo.context import AppContext
from aivo.controller.bake import Baker
from aivo.db.models import User

router = APIRouter(prefix="/admin", tags=["admin"])
log = logging.getLogger(__name__)
_tasks: set[asyncio.Task[Any]] = set()


def _spawn(coro: Any) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)

    def done(task: asyncio.Task[Any]) -> None:
        _tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("admin task failed: %s", task.exception())

    t.add_done_callback(done)


class ModelsIn(BaseModel):
    models: list[str]


@router.get("/cache")
async def cache_status(_: User = Depends(require_admin), app: AppContext = Depends(get_app)) -> dict[str, Any]:
    st = await CacheManager(app).status()
    st["bake_supported"] = Baker(app).supported()
    return st


@router.post("/cache/bake", status_code=202)
async def bake(_: User = Depends(require_admin), app: AppContext = Depends(get_app)) -> dict[str, str]:
    b = Baker(app)
    if not b.supported():
        raise HTTPException(409, "This cloud provider can't prepare machine images")
    run_id = await b.start()
    _spawn(b.run(run_id))
    return {"run_id": str(run_id)}


@router.post("/cache/mirror", status_code=202)
async def mirror(
    body: ModelsIn, _: User = Depends(require_admin), app: AppContext = Depends(get_app)
) -> dict[str, bool]:
    for m in body.models:
        if m not in app.catalog.models:
            raise HTTPException(422, f"unknown model {m}")
    cm = CacheManager(app)
    if cm._mirror is None:
        raise HTTPException(409, "Configure the Spaces mirror first (AIVO_SPACES_*)")
    _spawn(cm.mirror_models(body.models))
    return {"started": True}


@router.post("/cache/volumes", status_code=202)
async def volumes(_: User = Depends(require_admin), app: AppContext = Depends(get_app)) -> dict[str, list[str]]:
    return {"created": await CacheManager(app).ensure_volumes()}


@router.post("/cache/volumes/{region}/fill", status_code=202)
async def fill(
    region: str, body: ModelsIn, _: User = Depends(require_admin), app: AppContext = Depends(get_app)
) -> dict[str, bool]:
    _spawn(CacheManager(app).fill_volume(region, body.models))
    return {"started": True}
