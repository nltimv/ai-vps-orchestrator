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
from aivo.providers.regions import DO_REGIONS

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
async def get_settings(_: User = Depends(require_admin), app: AppContext = Depends(get_app),
                       s: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rs = await load_runtime(s)
    return {
        "settings": rs.model_dump(),
        "defaults": RuntimeSettings().model_dump(),
        "regions": [{"id": r.slug, "name": r.name} for r in DO_REGIONS.values()],
        "provider": app.provider.name,
        "spaces_configured": bool(app.env.spaces_bucket),
        "watchdog_token_configured": bool(app.env.do_watchdog_token),
    }


@router.put("")
async def put_settings(body: dict[str, Any], admin: User = Depends(require_admin), app: AppContext = Depends(get_app),
                       s: AsyncSession = Depends(get_session)) -> dict[str, Any]:
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
    if merged.idle_timeout_s < 60:
        raise HTTPException(422, "Idle shutdown must be at least 1 minute")
    data = merged.model_dump(mode="json")
    for k in body:
        await s.execute(insert(Setting).values(key=k, value=data[k], updated_by=admin.id)
                        .on_conflict_do_update(index_elements=[Setting.key],
                                               set_={"value": data[k], "updated_by": admin.id}))
    s.add(AuditLog(user_id=admin.id, action="settings.update", detail={"keys": sorted(body)}))
    await s.commit()
    app.invalidate_runtime()
    return {"settings": merged.model_dump()}
