"""Job creation, estimates and serialization (shared by API routes)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.api.schemas import AssetOut, EstimateOut, JobCreateIn, JobOut, MachineChoice
from aivo.catalog.loader import CatalogError
from aivo.catalog.presets import FriendlyParams, plan
from aivo.config import RuntimeSettings
from aivo.context import AppContext
from aivo.controller.budget import ensure_period, month_compute_spend, open_reservations
from aivo.controller.common import build_context, get_offers
from aivo.db.models import Asset, Instance, Job, User
from aivo.events.bus import emit
from aivo.placement.request import build_request
from aivo.placement.scorer import Candidate, rank
from aivo.providers.regions import region_meta
from aivo.safety import adult_rules


def asset_out(a: Asset, thumb: Asset | None = None) -> AssetOut:
    return AssetOut(
        id=a.id,
        media_type=a.media_type,
        width=a.width,
        height=a.height,
        duration_s=a.duration_s,
        bytes=a.plaintext_size,
        url=f"/api/v1/assets/{a.id}",
        download_url=f"/api/v1/assets/{a.id}?download=1",
        thumb_url=f"/api/v1/assets/{thumb.id}" if thumb else None,
    )


def machine_choice(c: Candidate) -> MachineChoice:
    return MachineChoice(
        key=c.key,
        instance_id=c.instance_id,
        gpu=c.offer.gpu_model,
        gpu_class=c.offer.gpu_class,
        vram_gb=c.offer.vram_gb,
        region=c.offer.region,
        region_name=region_meta(c.offer.region).name,
        price_hourly=c.offer.price_hourly,
        est_wait_s=round(c.wait_s, 1),
        est_cost_usd=round(c.cost_usd, 4),
        suitability=c.suitability,
        reasons=c.reasons,
        status="running" if ("running" in c.flags or "warm" in c.flags) else "new",
        engine_version="full" if c.variant.precision in ("bf16", "fp16") else "light",
    )


async def job_out(app: AppContext, s: AsyncSession, job: Job, user_name: str | None = None) -> JobOut:
    assets = (
        (
            await s.execute(
                select(Asset).where(Asset.job_id == job.id, Asset.deleted_at.is_(None)).order_by(Asset.output_name)
            )
        )
        .scalars()
        .all()
    )
    thumbs = {a.parent_asset_id: a for a in assets if a.role == "thumb"}
    outputs = [asset_out(a, thumbs.get(a.id)) for a in assets if a.role == "output"]
    reference = None
    if job.input_asset_ids:
        ref = (await s.execute(select(Asset).where(Asset.id == job.input_asset_ids[0]))).scalar_one_or_none()
        if ref:
            reference = asset_out(ref)
    model = app.catalog.models.get(job.model_id)
    machine = None
    if job.instance_id:
        inst = (await s.execute(select(Instance).where(Instance.id == job.instance_id))).scalar_one_or_none()
        if inst:
            machine = f"{inst.gpu_model} in {region_meta(inst.region).name}"
    kr = app.keyring
    return JobOut(
        id=job.id,
        user_id=job.user_id,
        user_name=user_name,
        kind=job.kind,
        mode=job.mode,
        model_id=job.model_id,
        model_name=model.manifest.display_name if model else job.model_id,
        prompt=kr.open(job.prompt_enc, job.id.bytes) or "",
        prompt_used=kr.open(job.prompt_used_enc, job.id.bytes),
        avoid=kr.open(job.avoid_enc, job.id.bytes),
        lyrics=kr.open(job.lyrics_enc, job.id.bytes),
        params=job.friendly_params,
        state=job.state,
        stage=job.stage,
        progress=job.progress,
        eta_s=job.eta_s,
        est_cost_usd=job.est_cost_usd,
        est_wait_s=job.est_wait_s,
        cost_usd=job.attributed_cost_usd,
        error=job.error_message,
        machine=machine,
        instance_id=job.instance_id,
        adult=bool(job.adult),
        seed=(job.resolved or {}).get("seed"),
        created_at=job.created_at,
        finished_at=job.finished_at,
        outputs=outputs,
        expected=((job.resolved or {}).get("output") or {}).get("count"),
        reference=reference,
    )


async def estimate(
    app: AppContext,
    s: AsyncSession,
    fp: FriendlyParams,
    preference: str | None,
    *,
    manual_offer_key: str | None = None,
    user: User | None = None,
) -> EstimateOut:
    rs = await app.runtime(s)
    try:
        pl = plan(app.catalog, fp, app.enabled_models(rs))
    except CatalogError as e:
        raise HTTPException(422, str(e)) from e
    if user is not None:  # same adult-content rules as creating, so the form says no right away
        await adult_rules(
            s,
            user,
            rs,
            adult_engine=pl.model.manifest.adult,
            prompt=fp.prompt,
            reference_asset_id=fp.reference_asset_id,
        )
    elif pl.model.manifest.adult:
        raise HTTPException(403, "Adult content isn't switched on for your account.")
    pref = preference or rs.preference_default
    req = build_request(pl, pref, hf_token=app.env.hf_token is not None)
    try:
        offers = await get_offers(app)
    except Exception:
        offers = []
    now = datetime.now(UTC)
    period = await ensure_period(s, rs, now, lock=False)
    ctx = await build_context(app, s, rs)
    cands = [] if period.locked else rank(req, offers, [], ctx)
    if manual_offer_key:
        cands = [c for c in cands if c.key == manual_offer_key] + [c for c in cands if c.key != manual_offer_key]
    spent = await month_compute_spend(s, period.period)
    reserved = await open_reservations(s)
    left = float(Decimal(str(rs.hard_cap_usd)) - spent - reserved)
    over_soft = float(spent + reserved) >= rs.soft_cap_usd
    locked = period.locked  # read before the rollback, which expires loaded rows
    await s.rollback()  # ensure_period may have inserted a row; estimates are read-only
    model_name = pl.model.manifest.display_name
    if not cands:
        msg = "No suitable GPU is available right now."
        if locked:
            msg = "This month's spending limit has been reached."
        return EstimateOut(
            model_id=pl.model.id,
            model_name=model_name,
            est_cost_usd=None,
            est_wait_s=None,
            machine=None,
            over_soft_cap=over_soft,
            budget_left_usd=left,
            message=msg,
        )
    top = cands[0]
    return EstimateOut(
        model_id=pl.model.id,
        model_name=model_name,
        est_cost_usd=round(top.cost_usd, 4),
        est_wait_s=round(top.wait_s, 1),
        machine=machine_choice(top),
        alternatives=[machine_choice(c) for c in cands[1:10]],
        over_soft_cap=over_soft,
        budget_left_usd=left,
    )


async def create_job(
    app: AppContext, s: AsyncSession, user: User, body: JobCreateIn, idem_key: str | None, rs: RuntimeSettings
) -> Job:
    if idem_key:
        existing = (
            await s.execute(select(Job).where(Job.user_id == user.id, Job.idempotency_key == idem_key))
        ).scalar_one_or_none()
        if existing:
            return existing
    try:
        pl = plan(app.catalog, body, app.enabled_models(rs))
    except CatalogError as e:
        raise HTTPException(422, str(e)) from e
    adult = await adult_rules(
        s,
        user,
        rs,
        adult_engine=pl.model.manifest.adult,
        prompt=body.prompt,
        reference_asset_id=body.reference_asset_id,
    )
    if body.reference_asset_id:
        ref = (
            await s.execute(select(Asset).where(Asset.id == body.reference_asset_id, Asset.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if ref is None or not ref.media_type.startswith("image/"):
            raise HTTPException(422, "The starting image could not be found")
    now = datetime.now(UTC)
    period = await ensure_period(s, rs, now, lock=False)
    if period.locked:
        raise HTTPException(402, "This month's spending limit has been reached. An admin can raise it in Settings.")
    spent = await month_compute_spend(s, period.period) + await open_reservations(s)
    if float(spent) >= rs.soft_cap_usd and not body.confirm_over_soft_cap:
        raise HTTPException(
            409,
            detail={
                "code": "confirm_soft_cap",
                "message": f"You've used ${float(spent):.2f} of your ${rs.soft_cap_usd:.0f} monthly "
                "budget warning level. Continue anyway?",
            },
        )
    jid = uuid.uuid4()
    friendly = body.model_dump(
        mode="json",
        exclude={"prompt", "avoid", "lyrics", "preference", "placement", "confirm_over_soft_cap", "model_id"},
    )
    friendly["model_id"] = pl.model.id
    friendly["mode"] = pl.mode
    kr = app.keyring
    job = Job(
        id=jid,
        user_id=user.id,
        idempotency_key=idem_key,
        kind=body.kind,
        mode=pl.mode,
        model_id=pl.model.id,
        prompt_enc=kr.seal(body.prompt, jid.bytes),
        avoid_enc=kr.seal(body.avoid, jid.bytes),
        lyrics_enc=kr.seal(body.lyrics, jid.bytes),
        friendly_params=friendly,
        input_asset_ids=[body.reference_asset_id] if body.reference_asset_id else [],
        adult=adult,
        preference=body.preference or rs.preference_default,
        placement_mode=body.placement.mode,
        manual_offer_key=body.placement.offer_key,
        allow_fallback=body.placement.allow_fallback,
        state="queued",
        stage="queued",
    )
    s.add(job)
    try:
        await s.flush()
    except IntegrityError as e:
        await s.rollback()
        raise HTTPException(409, "Duplicate request") from e
    await emit(s, scope="job", type="job.created", ref_id=jid, user_id=user.id, payload={"state": "queued"})
    return job
