"""GPUs: one RunPod queue endpoint per GPU type, its queue and workers, and the options for a typical request."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.api.deps import current_user, get_app, get_session, require_admin
from aivo.catalog.presets import FriendlyParams
from aivo.context import AppContext
from aivo.controller.common import build_context, get_offers
from aivo.controller.endpoints import EndpointManager
from aivo.db.models import CostAccrual, GpuEndpoint, Instance, Job, User
from aivo.placement.request import build_request
from aivo.placement.scorer import rank
from aivo.providers.base import ProviderError
from aivo.providers.regions import region_meta
from aivo.services.jobs import machine_choice

router = APIRouter(prefix="/machines", tags=["machines"])

ACTIVE = ("assigned", "preparing", "running", "fetching")


class KeepWarmIn(BaseModel):
    minutes: int = Field(30, ge=0, le=12 * 60)


async def _health(app: AppContext, runpod_id: str | None) -> dict[str, Any] | None:
    if not runpod_id:
        return None
    try:
        return await asyncio.wait_for(app.runpod.health(runpod_id), 5)
    except Exception:
        return None


@router.get("")
async def list_gpus(
    user: User = Depends(current_user), app: AppContext = Depends(get_app), s: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rs = await app.runtime(s)
    now = datetime.now(UTC)
    endpoints = (await s.execute(select(GpuEndpoint).order_by(GpuEndpoint.price_hourly))).scalars().all()
    jobs_by_ep = dict(
        (
            await s.execute(
                select(Job.endpoint_id, func.count())
                .where(Job.state.in_(ACTIVE), Job.endpoint_id.is_not(None))
                .group_by(Job.endpoint_id)
            )
        ).all()
    )
    healths = await asyncio.gather(*(_health(app, e.runpod_id) for e in endpoints))
    gpus = []
    for e, h in zip(endpoints, healths, strict=True):
        hw = (h or {}).get("workers") or {}
        hj = (h or {}).get("jobs") or {}
        gpus.append(
            {
                "gpu_type": e.gpu_type,
                "gpu": e.gpu_name,
                "gpu_class": e.gpu_class,
                "vram_gb": e.vram_gb,
                "price_hourly": float(e.price_hourly),
                "status": e.status,
                "error": e.error if user.role == "admin" else None,
                "keep_warm_until": e.keep_warm_until if e.keep_warm_until and e.keep_warm_until > now else None,
                "last_used_at": e.last_used_at,
                "workers_running": hw.get("running"),
                "workers_idle": hw.get("idle"),
                "jobs_waiting": hj.get("inQueue"),
                "jobs_active": jobs_by_ep.get(e.id, 0),
            }
        )
    since = now - timedelta(hours=24)
    rows = (
        await s.execute(
            select(
                Instance,
                func.coalesce(func.sum(CostAccrual.usd), 0),
                func.coalesce(func.sum(CostAccrual.billed_seconds), 0),
            )
            .outerjoin(CostAccrual, CostAccrual.instance_id == Instance.id)
            .where((Instance.state != "destroyed") | (Instance.destroy_confirmed_at > since))
            .group_by(Instance.id)
            .order_by(Instance.created_at.desc())
            .limit(50)
        )
    ).all()
    current = dict(
        (
            await s.execute(select(Job.instance_id, Job.id).where(Job.state.in_(ACTIVE), Job.instance_id.is_not(None)))
        ).all()
    )
    workers = [
        {
            "id": i.id,
            "worker_id": i.provider_instance_id,
            "gpu": i.gpu_model,
            "gpu_type": i.size_slug,
            "region_name": region_meta(i.region).name,
            "price_hourly": float(i.price_hourly),
            "state": {"busy": "working", "ready": "waiting"}.get(i.state, "stopped"),
            "started_at": i.create_accepted_at,
            "idle_since": i.idle_since,
            "stops_at": i.idle_since + timedelta(seconds=rs.keep_warm_s)
            if i.state == "ready" and i.idle_since
            else None,
            "ended_at": i.destroy_confirmed_at,
            "cost_usd": round(float(usd), 4),
            "hours": round(float(secs) / 3600, 3),
            "current_job_id": current.get(i.id) if user.role == "admin" else None,  # creations are private
        }
        for i, usd, secs in rows
        if i.provider == "runpod"
    ]
    return {
        "gpus": gpus,
        "workers": workers,
        "max_workers_per_gpu": rs.max_workers_per_gpu,
        "keep_warm_s": rs.keep_warm_s,
        "offers_error": app.offers.error,
    }


@router.post("/gpus/{gpu_type:path}/keep-warm")
async def keep_warm(
    gpu_type: str,
    body: KeepWarmIn,
    _: User = Depends(require_admin),
    app: AppContext = Depends(get_app),
) -> dict[str, Any]:
    """Keep one worker of this GPU type running (billed all the time) for a while: no start-up wait."""
    offers = await get_offers(app)
    if not any(o.size_slug == gpu_type for o in offers):
        raise HTTPException(404, "unknown GPU type")
    async with app.sessionmaker() as s, s.begin():
        exists = (await s.execute(select(GpuEndpoint.id).where(GpuEndpoint.gpu_type == gpu_type))).scalar_one_or_none()
    try:
        if exists is None:
            offer = next(o for o in offers if o.size_slug == gpu_type)
            await EndpointManager(app).ensure(offer)
        row = await EndpointManager(app).set_keep_warm(gpu_type, body.minutes)
    except ProviderError as e:
        raise HTTPException(502, f"RunPod: {e}") from e
    return {"gpu_type": gpu_type, "keep_warm_until": row.keep_warm_until}


@router.get("/offers")
async def offers(
    kind: str = "image",
    model_id: str | None = None,
    quality: str = "standard",
    preference: str | None = None,
    _: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """All GPU types ranked for a typical request, with plain-language reasons."""
    from aivo.catalog.presets import plan

    rs = await app.runtime(s)
    fp = FriendlyParams(kind=kind, prompt="x", model_id=model_id, quality=quality)  # type: ignore[arg-type]
    pl = plan(app.catalog, fp, app.enabled_models(rs))
    req = build_request(pl, preference or rs.preference_default, hf_token=app.env.hf_token is not None)
    try:
        all_offers = await get_offers(app)
        error = app.offers.error
    except Exception as e:
        all_offers, error = [], str(e)
    ctx = await build_context(app, s, rs)
    cands = rank(req, all_offers, [], ctx)
    ranked = {c.offer.key for c in cands}
    unsuitable = [
        {
            "key": o.key,
            "gpu": o.gpu_model,
            "vram_gb": o.vram_gb,
            "region": o.region,
            "region_name": region_meta(o.region).name,
            "price_hourly": o.price_hourly,
            "why": "Not enough memory for this engine"
            if o.vram_gb < min(v.min_vram_gb for v in req.variants)
            else "None free in the allowed data centers"
            if not o.available
            else "Excluded by settings",
        }
        for o in all_offers
        if o.key not in ranked
    ]
    return {
        "model_id": pl.model.id,
        "model_name": pl.model.manifest.display_name,
        "choices": [machine_choice(c).model_dump() for c in cands],
        "unsuitable": unsuitable,
        "offers_error": error,
    }
