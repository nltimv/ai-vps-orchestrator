from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.api.deps import current_user, get_app, get_session, require_admin
from aivo.api.schemas import InstanceOut, KeepWarmIn, MachineChoice, WarmupIn
from aivo.catalog.presets import FriendlyParams
from aivo.context import AppContext
from aivo.controller.budget import admit, reservation_for
from aivo.controller.common import build_context, get_offers, live_view
from aivo.controller.lifecycle import transition_instance
from aivo.controller.scheduler import BILLING_STATES, MAX_CANDIDATES, _cand_json
from aivo.db.models import CostAccrual, Event, Instance, Job, User
from aivo.events.bus import emit
from aivo.placement.request import build_request
from aivo.placement.scorer import rank
from aivo.providers.base import instance_name
from aivo.providers.regions import region_meta
from aivo.services.jobs import machine_choice

router = APIRouter(prefix="/machines", tags=["machines"])


async def _instance_out(s: AsyncSession, i: Instance, idle_timeout_s: int, with_timeline: bool) -> InstanceOut:
    now = datetime.now(UTC)
    cost = (
        await s.execute(select(func.coalesce(func.sum(CostAccrual.usd), 0)).where(CostAccrual.instance_id == i.id))
    ).scalar_one()
    job_id = (
        await s.execute(
            select(Job.id)
            .where(Job.instance_id == i.id, Job.state.in_(("assigned", "preparing", "running", "fetching")))
            .limit(1)
        )
    ).scalar_one_or_none()
    idle_at = None
    if i.state == "ready" and i.idle_since:
        idle_at = i.idle_since + timedelta(seconds=idle_timeout_s)
        if i.keep_warm_until and i.keep_warm_until > idle_at:
            idle_at = i.keep_warm_until
    timeline = []
    if with_timeline:
        evs = (
            (await s.execute(select(Event).where(Event.scope == "instance", Event.ref_id == i.id).order_by(Event.id)))
            .scalars()
            .all()
        )
        timeline = [{"at": e.at, "state": e.payload.get("state"), "detail": e.payload} for e in evs]
    end = i.destroy_confirmed_at or now
    return InstanceOut(
        id=i.id,
        name=i.name,
        state=i.state,
        gpu=i.gpu_model,
        gpu_class=i.gpu_class,
        vram_gb=i.vram_gb,
        region=i.region,
        region_name=region_meta(i.region).name,
        price_hourly=float(i.price_hourly),
        created_at=i.created_at,
        ready_at=i.ready_at,
        idle_since=i.idle_since,
        keep_warm_until=i.keep_warm_until,
        idle_shutdown_at=idle_at,
        uptime_s=(end - i.create_accepted_at).total_seconds() if i.create_accepted_at else None,
        cost_so_far_usd=float(cost),
        models_loaded=list(i.models_loaded or []),
        current_job_id=job_id,
        failure_reason=i.failure_reason,
        timeline=timeline,
    )


@router.get("")
async def list_machines(
    recent: int = 10,
    _: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, list[InstanceOut]]:
    rs = await app.runtime(s)
    live = (
        (await s.execute(select(Instance).where(Instance.state != "destroyed").order_by(Instance.created_at.desc())))
        .scalars()
        .all()
    )
    past = (
        (
            await s.execute(
                select(Instance).where(Instance.state == "destroyed").order_by(Instance.created_at.desc()).limit(recent)
            )
        )
        .scalars()
        .all()
    )
    return {
        "live": [await _instance_out(s, i, rs.idle_timeout_s, True) for i in live],
        "recent": [await _instance_out(s, i, rs.idle_timeout_s, False) for i in past],
    }


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
    """All machine options ranked for a typical request, with plain-language reasons."""
    from aivo.catalog.presets import plan

    rs = await app.runtime(s)
    fp = FriendlyParams(kind=kind, prompt="x", model_id=model_id, quality=quality)  # type: ignore[arg-type]
    pl = plan(app.catalog, fp, app.enabled_models(rs))
    req = build_request(pl, preference or rs.preference_default)
    try:
        all_offers = await get_offers(app)
        error = app.offers.error
    except Exception as e:
        all_offers, error = [], str(e)
    _, live = await live_view(s, app)
    ctx = await build_context(app, s, rs, allow_new=True)
    cands = rank(req, all_offers, live, ctx)
    ranked_keys = {c.offer.key for c in cands if c.instance_id is None}
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
            else "Not compatible"
            if o.vendor not in {x for v in req.variants for x in v.vendors}
            else "Excluded by settings or recently sold out",
        }
        for o in all_offers
        if o.key not in ranked_keys and o.gpu_count == 1
    ]
    return {
        "model_id": pl.model.id,
        "model_name": pl.model.manifest.display_name,
        "choices": [machine_choice(c).model_dump() for c in cands],
        "unsuitable": unsuitable,
        "offers_error": error,
    }


@router.post("/warmup", status_code=202)
async def warmup(
    body: WarmupIn,
    user: User = Depends(require_admin),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> MachineChoice:
    """Start a machine ahead of time so the next creation doesn't wait for start-up."""
    from aivo.catalog.presets import plan

    rs = await app.runtime(s)
    fp = FriendlyParams(kind=body.kind, prompt="warmup", model_id=body.model_id, quality=body.quality)
    pl = plan(app.catalog, fp, app.enabled_models(rs))
    req = build_request(pl, body.preference or rs.preference_default)
    live_count = (
        await s.execute(select(func.count()).select_from(Instance).where(Instance.state.in_(BILLING_STATES)))
    ).scalar_one()
    if live_count >= rs.max_instances:
        raise HTTPException(409, "The maximum number of machines is already running")
    ctx = await build_context(app, s, rs, allow_new=True)
    cands = [c for c in rank(req, await get_offers(app), [], ctx) if c.instance_id is None]
    if body.offer_key:
        cands = [c for c in cands if c.key == body.offer_key] + [c for c in cands if c.key != body.offer_key]
    if not cands:
        raise HTTPException(409, "No suitable machine is available right now")
    top = cands[0]
    now = datetime.now(UTC)
    reservation = reservation_for(top.offer.price_hourly, rs.idle_timeout_s, rs)
    adm = await admit(s, rs, now, reservation)
    if not adm.ok:
        raise HTTPException(402, "This month's spending limit has been reached")
    iid = uuid.uuid4()
    o = top.offer
    cand_json = [_cand_json(c) for c in cands[:MAX_CANDIDATES]]
    cand_json[0]["prefetch_model"] = pl.model.id
    cand_json[0]["prefetch_mode"] = pl.mode
    s.add(
        Instance(
            id=iid,
            name=instance_name(app.deployment_id, iid),
            provider=app.provider.name,
            offer_key=o.key,
            size_slug=o.size_slug,
            region=o.region,
            gpu_class=o.gpu_class or "unknown",
            gpu_model=o.gpu_model,
            vendor=o.vendor,
            vram_gb=o.vram_gb,
            ram_gb=o.ram_gb,
            price_hourly=o.price_hourly,
            state="pending",
            manual=bool(body.offer_key),
            reserved_usd=reservation,
            candidates=cand_json,
        )
    )
    await s.flush()
    await emit(
        s,
        scope="instance",
        type="instance.state",
        ref_id=iid,
        payload={"state": "pending", "warmup": True, "by": str(user.id)},
    )
    await s.commit()
    if app.controller:
        app.controller.spawn_provisioner(iid)
    return machine_choice(top)


@router.post("/{instance_id}/keep-warm")
async def keep_warm(
    instance_id: uuid.UUID, body: KeepWarmIn, _: User = Depends(require_admin), s: AsyncSession = Depends(get_session)
) -> dict[str, object]:
    until = datetime.now(UTC) + timedelta(minutes=body.minutes) if body.minutes else None
    r = await s.execute(
        update(Instance)
        .where(
            Instance.id == instance_id,
            Instance.state.in_(("pending", "creating", "provisioning", "booting", "ready", "busy")),
        )
        .values(keep_warm_until=until)
        .returning(Instance.id)
    )
    if r.first() is None:
        raise HTTPException(404, "No such running machine")
    await emit(
        s,
        scope="instance",
        type="instance.keep_warm",
        ref_id=instance_id,
        payload={"until": until.isoformat() if until else None},
    )
    await s.commit()
    return {"keep_warm_until": until}


@router.post("/{instance_id}/shutdown")
async def shutdown(
    instance_id: uuid.UUID,
    _: User = Depends(require_admin),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    inst = (await s.execute(select(Instance).where(Instance.id == instance_id).with_for_update())).scalar_one_or_none()
    if inst is None or inst.state == "destroyed":
        raise HTTPException(404, "No such running machine")
    now = datetime.now(UTC)
    if inst.state == "busy":
        inst.drain_reason = "manual"  # finish the current creation first
    elif inst.state == "ready":
        await transition_instance(s, inst.id, "ready", "draining", drain_reason="manual", draining_at=now)
    elif inst.state in ("pending", "creating") and not inst.provider_instance_id:
        await transition_instance(s, inst.id, inst.state, "destroyed", outcome="ok", failure_reason="cancelled")
    elif inst.state != "destroying":
        await transition_instance(s, inst.id, inst.state, "destroying", destroy_requested_at=now, drain_reason="manual")
    await s.commit()
    if app.controller:
        app.controller.wake()
    return {"ok": True}
