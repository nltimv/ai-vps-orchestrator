from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.api.deps import get_app, get_session, require_admin
from aivo.config import RuntimeSettings
from aivo.context import AppContext, load_runtime
from aivo.db.models import AuditLog, Setting, User

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
async def get_settings(
    _: User = Depends(require_admin), app: AppContext = Depends(get_app), s: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rs = await load_runtime(s)
    return {
        "settings": rs.model_dump(),
        "defaults": RuntimeSettings().model_dump(),
        "provider": app.runpod.name,
        "runpod": {
            "network_volume": bool(app.env.runpod_network_volume_id),
            "countries": app.env.runpod_countries,
            "data_centers": app.env.runpod_data_centers,
            "gpu_types": app.env.runpod_gpu_types,
            "worker_api_url": app.env.worker_api_url or app.env.public_url,
        },
    }


@router.put("")
async def put_settings(
    body: dict[str, Any],
    admin: User = Depends(require_admin),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    current = (await load_runtime(s)).model_dump()
    unknown = set(body) - set(current)
    if unknown:
        raise HTTPException(422, f"Unknown settings: {sorted(unknown)}")
    try:
        merged = RuntimeSettings.model_validate({**current, **body})
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
    if merged.hard_cap_usd < merged.soft_cap_usd:
        raise HTTPException(422, "The spending limit must be at least the warning level")
    if not 5 <= merged.keep_warm_s <= 3600:
        raise HTTPException(422, "Workers wait between 5 seconds and 1 hour for a next job")
    if not 1 <= merged.max_workers_per_gpu <= 20:
        raise HTTPException(422, "Workers per GPU type: between 1 and 20")
    data = merged.model_dump(mode="json")
    for k in body:
        await s.execute(
            insert(Setting)
            .values(key=k, value=data[k], updated_by=admin.id)
            .on_conflict_do_update(index_elements=[Setting.key], set_={"value": data[k], "updated_by": admin.id})
        )
    s.add(AuditLog(user_id=admin.id, action="settings.update", detail={"keys": sorted(body)}))
    await s.commit()
    app.invalidate_runtime()
    return {"settings": merged.model_dump()}
