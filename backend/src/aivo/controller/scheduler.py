"""Match queued jobs to machines; start new machines when that is the best option."""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.catalog.loader import CatalogError
from aivo.catalog.presets import FriendlyParams, Plan, plan, workflow_params
from aivo.config import RuntimeSettings
from aivo.context import AppContext
from aivo.controller.budget import admit, ensure_period, reservation_for
from aivo.controller.common import build_context, get_offers, live_view, offer_from_instance, utcnow
from aivo.controller.lifecycle import transition_instance, transition_job
from aivo.db.models import Asset, Instance, Job
from aivo.placement.request import build_request
from aivo.placement.scorer import Candidate, Context, LiveInstance, Request, rank
from aivo.providers.base import GpuOffer, instance_name

log = logging.getLogger(__name__)

BILLING_STATES = ("pending", "creating", "provisioning", "booting", "ready", "busy", "draining")
MAX_CANDIDATES = 8
BUDGET_MESSAGE = "This month's spending limit has been reached. An admin can raise it in Settings."


def friendly_from_job(app: AppContext, job: Job) -> FriendlyParams:
    kr = app.keyring
    return FriendlyParams.model_validate(
        {
            **job.friendly_params,
            "prompt": kr.open(job.prompt_enc, job.id.bytes) or "",
            "avoid": kr.open(job.avoid_enc, job.id.bytes),
            "lyrics": kr.open(job.lyrics_enc, job.id.bytes),
        }
    )


def job_request(app: AppContext, job: Job, rs: RuntimeSettings) -> tuple[FriendlyParams, Plan, Request]:
    fp = friendly_from_job(app, job)
    pl = plan(app.catalog, fp, app.enabled_models(rs) | {job.model_id})
    req = build_request(pl, job.preference, min_vram_override=job.min_vram_gb)
    return fp, pl, req


def apply_manual(job: Job, cands: list[Candidate]) -> list[Candidate]:
    if job.placement_mode != "manual" or not job.manual_offer_key:
        return cands
    key = job.manual_offer_key
    pinned = [c for c in cands if c.offer.key == key]
    if not job.allow_fallback:
        return pinned
    rest = [c for c in cands if c not in pinned]
    return pinned + rest


class Scheduler:
    def __init__(self, app: AppContext):
        self.app = app

    async def plan_once(self) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Returns (instances to provision, jobs to run). Caller spawns tasks after commit."""
        app = self.app
        rs = await app.runtime(fresh=True)
        try:
            offers = await get_offers(app)
        except Exception as e:
            log.warning("could not list offers: %s", e)
            offers = []
        to_provision: list[uuid.UUID] = []
        to_run: list[uuid.UUID] = []
        async with app.sessionmaker() as s, s.begin():
            now = utcnow()
            # 1) machines that just became ready: give them the job that was waiting for them
            ready = (
                (
                    await s.execute(
                        select(Instance)
                        .where(Instance.state == "ready", Instance.drain_reason.is_(None))
                        .order_by(Instance.idle_since)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for inst in ready:
                job = (
                    await s.execute(
                        select(Job)
                        .where(
                            Job.instance_id == inst.id,
                            Job.state.in_(("provisioning", "queued")),
                            Job.cancel_requested.is_(False),
                        )
                        .order_by(Job.created_at)
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                if job is not None and await self._assign(s, job, inst, rs, None):
                    to_run.append(job.id)

            # 2) everything still unplaced
            jobs = (
                (
                    await s.execute(
                        select(Job)
                        .where(
                            Job.state.in_(("queued", "waiting_capacity")),
                            Job.instance_id.is_(None),
                            Job.cancel_requested.is_(False),
                            (Job.next_attempt_at.is_(None)) | (Job.next_attempt_at <= now),
                        )
                        .order_by(Job.created_at)
                        .limit(25)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            if not jobs:
                return to_provision, to_run
            rows, live = await live_view(s, app)
            by_id = {r.id: r for r in rows}
            live_count = (
                await s.execute(select(func.count()).select_from(Instance).where(Instance.state.in_(BILLING_STATES)))
            ).scalar_one()
            period = await ensure_period(s, rs, now, lock=False)
            ctx = await build_context(app, s, rs, allow_new=live_count < rs.max_instances and not period.locked)
            for job in jobs:
                try:
                    _fp, _pl, req = job_request(app, job, rs)
                except CatalogError as e:
                    await transition_job(
                        s, job.id, job.state, "failed", error_code="bad_request", error_message=str(e), stage="failed"
                    )
                    continue
                # reserved-but-busy machines: only the ones with spare queue capacity
                cands = apply_manual(job, rank(req, offers, live, ctx))
                if not cands:
                    fits_new = False
                    if live_count >= rs.max_instances and not period.locked:
                        ctx.allow_new = True
                        fits_new = bool(rank(req, offers, [], ctx))
                        ctx.allow_new = False
                    await self._no_candidates(s, job, rs, live_count, ctx, fits_new)
                    continue
                top = cands[0]
                if top.instance_id is not None:
                    inst = by_id.get(top.instance_id)
                    if inst is None:
                        continue
                    if inst.state == "ready" and not await self._has_reserved(s, inst.id):
                        if await self._assign(s, job, inst, rs, top):
                            to_run.append(job.id)
                            live = [x for x in live if x.instance_id != inst.id] + [
                                LiveInstance(
                                    **{
                                        **asdict_live(_find(live, inst.id)),
                                        "state": "busy",
                                        "queue_s": top.times.get("run", 0),
                                    }
                                )
                            ]
                    else:
                        stage = (
                            "starting_machine"
                            if inst.state in ("pending", "creating", "provisioning", "booting")
                            else "waiting_in_line"
                        )
                        await transition_job(
                            s,
                            job.id,
                            job.state,
                            "provisioning" if stage == "starting_machine" else "queued",
                            instance_id=inst.id,
                            stage=stage,
                            est_cost_usd=top.cost_usd,
                            est_wait_s=top.wait_s,
                            event_payload={"machine": _machine_label(top.offer)},
                        )
                        live = _bump_queue(live, inst.id, top.times.get("run", 0) + top.times.get("load", 0))
                    continue
                # new machine
                new_cands = [c for c in cands if c.instance_id is None][:MAX_CANDIDATES]
                reservation = reservation_for(top.offer.price_hourly, top.wait_s, rs)
                adm = await admit(s, rs, now, reservation)
                if not adm.ok:
                    await transition_job(
                        s,
                        job.id,
                        job.state,
                        "failed",
                        error_code="budget",
                        stage="failed",
                        error_message=BUDGET_MESSAGE,
                    )
                    continue
                iid = uuid.uuid4()
                o = top.offer
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
                        requested_by_job_id=job.id,
                        manual=job.placement_mode == "manual",
                        reserved_usd=reservation,
                        candidates=[_cand_json(c) for c in new_cands],
                        attempt_index=0,
                    )
                )
                await s.flush()
                await transition_job(
                    s,
                    job.id,
                    job.state,
                    "provisioning",
                    instance_id=iid,
                    stage="starting_machine",
                    est_cost_usd=top.cost_usd,
                    est_wait_s=top.wait_s,
                    event_payload={"machine": _machine_label(o)},
                )
                to_provision.append(iid)
                live_count += 1
                ctx.allow_new = live_count < rs.max_instances
                live = [
                    *live,
                    LiveInstance(
                        instance_id=iid,
                        offer=o,
                        state="pending",
                        queue_s=top.times.get("run", 0),
                        remaining_boot_s=top.times.get("create", 0) + top.times.get("boot", 0),
                    ),
                ]
        return to_provision, to_run

    async def _has_reserved(self, s: AsyncSession, inst_id: uuid.UUID) -> bool:
        n = (
            await s.execute(
                select(func.count())
                .select_from(Job)
                .where(
                    Job.instance_id == inst_id,
                    Job.state.in_(("provisioning", "queued", "assigned", "preparing", "running", "fetching")),
                )
            )
        ).scalar_one()
        return n > 0

    async def _no_candidates(
        self, s: AsyncSession, job: Job, rs: RuntimeSettings, live_count: int, ctx: Context, fits_new: bool
    ) -> None:
        now = utcnow()
        if live_count >= rs.max_instances:
            # an idle machine that can't run this job is blocking the slot: replace it
            idle = (
                (
                    await s.execute(
                        select(Instance)
                        .where(
                            Instance.state == "ready",
                            Instance.drain_reason.is_(None),
                            (Instance.keep_warm_until.is_(None)) | (Instance.keep_warm_until < now),
                        )
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for inst in idle if fits_new else []:
                if not await self._has_reserved(s, inst.id):
                    await transition_instance(
                        s,
                        inst.id,
                        "ready",
                        "draining",
                        drain_reason="make_room",
                        draining_at=now,
                        event_payload={"reason": "make_room"},
                    )
                    break
            if job.stage != "waiting_in_line":
                await transition_job(s, job.id, job.state, "queued", stage="waiting_in_line")
            return
        wait_until = job.capacity_wait_until or now + timedelta(seconds=rs.capacity_wait_max_s)
        if now >= wait_until:
            await transition_job(
                s,
                job.id,
                job.state,
                "failed",
                error_code="no_capacity",
                stage="failed",
                error_message="No suitable GPU machine is available right now. Please try again later.",
            )
            return
        await transition_job(
            s,
            job.id,
            job.state,
            "waiting_capacity",
            stage="waiting_for_gpu",
            capacity_wait_until=wait_until,
            next_attempt_at=min(now + timedelta(seconds=min(300, 60 + 60 * job.attempt)), wait_until),
        )

    async def _assign(
        self, s: AsyncSession, job: Job, inst: Instance, rs: RuntimeSettings, cand: Candidate | None
    ) -> bool:
        app = self.app
        try:
            fp, pl, req = job_request(app, job, rs)
        except CatalogError as e:
            await transition_job(
                s, job.id, job.state, "failed", error_code="bad_request", error_message=str(e), stage="failed"
            )
            return False
        variant_id = cand.variant.variant_id if cand else None
        if variant_id is None:
            live = LiveInstance(
                inst.id,
                offer_from_instance(inst),
                "ready",
                frozenset(inst.files_present or []),
                tuple(inst.models_loaded or []),
            )
            ranked = rank(
                req,
                [],
                [live],
                Context(allow_new=False, gpu_perf={g.id: g.perf for g in app.catalog.gpu_classes.values()}),
            )
            if not ranked:
                # this machine can't run it (e.g. too small after an out-of-memory bump): release reservation
                await transition_job(s, job.id, job.state, "queued", instance_id=None, stage="queued")
                return False
            cand = ranked[0]
            variant_id = cand.variant.variant_id
        variant = pl.model.manifest.variant(variant_id)
        inputs: list[dict[str, Any]] = []
        prefix = f"aivo_{job.id.hex[:10]}"
        params = workflow_params(app.catalog, pl, fp, variant, seed=random.randrange(2**32), prefix=prefix)
        for aid in job.input_asset_ids or []:
            a = (await s.execute(select(Asset).where(Asset.id == aid))).scalar_one_or_none()
            if a is None:
                continue
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(a.media_type, ".png")
            name = f"ref-{job.id.hex[:12]}{ext}"
            inputs.append({"name": name, "asset_id": str(a.id), "storage_path": a.storage_path})
            params["reference_image"] = name
        resolved = {
            "variant_id": variant_id,
            "mode": pl.mode,
            "quality": pl.quality,
            "params": params,
            "output": pl.output.model_dump(),
            "units": pl.units,
            "inputs": inputs,
            "seed": params.get("seed"),
            "timeout_s": 3600,
        }
        if not await transition_instance(s, inst.id, "ready", "busy", idle_since=None):
            return False
        ok = await transition_job(
            s,
            job.id,
            job.state,
            "assigned",
            instance_id=inst.id,
            variant_id=variant_id,
            resolved=resolved,
            stage="loading_model",
            assigned_at=utcnow(),
            est_cost_usd=cand.cost_usd if cand else job.est_cost_usd,
            est_wait_s=cand.wait_s if cand else job.est_wait_s,
            event_payload={"machine": _machine_label(offer_from_instance(inst))},
        )
        return ok


def _cand_json(c: Candidate) -> dict[str, Any]:
    return {
        "offer": asdict(c.offer),
        "variant_id": c.variant.variant_id,
        "score": round(c.score, 4),
        "wait_s": round(c.wait_s, 1),
        "cost_usd": round(c.cost_usd, 4),
        "reasons": c.reasons,
    }


def _machine_label(o: GpuOffer) -> str:
    from aivo.providers.regions import region_meta

    return f"{o.gpu_model} in {region_meta(o.region).name}"


def _find(live: list[LiveInstance], iid: uuid.UUID) -> LiveInstance:
    return next(x for x in live if x.instance_id == iid)


def asdict_live(x: LiveInstance) -> dict[str, Any]:
    return {f: getattr(x, f) for f in LiveInstance.__dataclass_fields__}


def _bump_queue(live: list[LiveInstance], iid: uuid.UUID, extra: float) -> list[LiveInstance]:
    out = []
    for x in live:
        if x.instance_id == iid:
            out.append(LiveInstance(**{**asdict_live(x), "queue_s": x.queue_s + extra}))
        else:
            out.append(x)
    return out
