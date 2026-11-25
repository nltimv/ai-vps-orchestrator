"""Pick a GPU type for each waiting job and hand it to a job runner (which submits it to RunPod's queue).

RunPod's queue does the rest: it starts workers for waiting jobs (up to "workers per GPU type") and gives each
worker one job at a time. aivo only chooses where a job goes, keeps the budget, and falls back to the next GPU
type when no worker takes the job in time.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.catalog.loader import CatalogError
from aivo.config import RuntimeSettings
from aivo.context import AppContext
from aivo.controller.budget import admit, ensure_period, reservation_for
from aivo.controller.common import build_context, get_offers, utcnow
from aivo.controller.jobspec import job_request, resolve
from aivo.controller.lifecycle import transition_job
from aivo.db.models import Job
from aivo.placement.scorer import Candidate, rank
from aivo.providers.base import GpuOffer

log = logging.getLogger(__name__)

MAX_CANDIDATES = 4
BUDGET_MESSAGE = "This month's spending limit has been reached. An admin can raise it in Settings."
NO_GPU_MESSAGE = "No suitable GPU is available right now. Please try again later."


def apply_manual(job: Job, cands: list[Candidate]) -> list[Candidate]:
    if job.placement_mode != "manual" or not job.manual_offer_key:
        return cands
    key = job.manual_offer_key
    pinned = [c for c in cands if c.offer.key == key]
    if not job.allow_fallback:
        return pinned
    return pinned + [c for c in cands if c not in pinned]


def cand_json(c: Candidate) -> dict[str, Any]:
    return {
        "offer": asdict(c.offer),
        "variant_id": c.variant.variant_id,
        "wait_s": round(c.wait_s, 1),
        "cost_usd": round(c.cost_usd, 4),
    }


def machine_label(o: GpuOffer) -> str:
    from aivo.providers.regions import region_meta

    return f"{o.gpu_model} in {region_meta(o.region).name}"


class Scheduler:
    def __init__(self, app: AppContext):
        self.app = app

    async def plan_once(self) -> list[uuid.UUID]:
        """Jobs that got a GPU type; the caller starts their runners after commit."""
        app = self.app
        rs = await app.runtime(fresh=True)
        try:
            offers = await get_offers(app)
        except Exception as e:
            log.warning("could not list GPU types: %s", e)
            offers = []
        to_run: list[uuid.UUID] = []
        async with app.sessionmaker() as s, s.begin():
            now = utcnow()
            jobs = (
                (
                    await s.execute(
                        select(Job)
                        .where(
                            Job.state.in_(("queued", "waiting_capacity")),
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
                return to_run
            period = await ensure_period(s, rs, now, lock=False)
            ctx = await build_context(app, s, rs)
            for job in jobs:
                if period.locked:
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
                try:
                    fp, pl, req = job_request(app, job, rs)
                except CatalogError as e:
                    await transition_job(
                        s, job.id, job.state, "failed", error_code="bad_request", error_message=str(e), stage="failed"
                    )
                    continue
                cands = apply_manual(job, rank(req, offers, [], ctx))[:MAX_CANDIDATES]
                if not cands:
                    await self._no_gpu(s, job, rs)
                    continue
                top = cands[0]
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
                resolved = await resolve(app, s, job, fp, pl, top.variant.variant_id, rs)
                ok = await transition_job(
                    s,
                    job.id,
                    job.state,
                    "assigned",
                    gpu_type=top.offer.size_slug,
                    candidates=[cand_json(c) for c in cands],
                    variant_id=top.variant.variant_id,
                    resolved=resolved,
                    reserved_usd=reservation,
                    stage="waiting_for_gpu",
                    progress=0.0,
                    assigned_at=now,
                    runpod_job_id=None,
                    ticket_hash=None,
                    capacity_wait_until=job.capacity_wait_until or now + timedelta(seconds=rs.capacity_wait_max_s),
                    est_cost_usd=top.cost_usd,
                    est_wait_s=top.wait_s,
                    event_payload={"machine": machine_label(top.offer)},
                )
                if ok:
                    to_run.append(job.id)
        return to_run

    async def _no_gpu(self, s: AsyncSession, job: Job, rs: RuntimeSettings) -> None:
        now = utcnow()
        wait_until = job.capacity_wait_until or now + timedelta(seconds=rs.capacity_wait_max_s)
        if now >= wait_until:
            await transition_job(
                s, job.id, job.state, "failed", error_code="no_capacity", stage="failed", error_message=NO_GPU_MESSAGE
            )
            return
        await transition_job(
            s,
            job.id,
            job.state,
            "waiting_capacity",
            stage="waiting_for_gpu",
            capacity_wait_until=wait_until,
            next_attempt_at=min(now + timedelta(seconds=60), wait_until),
        )
