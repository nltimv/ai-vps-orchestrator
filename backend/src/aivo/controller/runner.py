"""One task per job sent to RunPod: submit it to the GPU type's queue and watch it until it ends.

The worker itself reports progress, delivers the results and says when it's done (``api/routes/worker.py``).
This runner covers what the worker can't: no worker took the job in time (-> the next GPU type), the
worker vanished (RunPod retries; after that the job fails), the user cancelled, or RunPod lost the job.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from aivo.context import AppContext
from aivo.controller.common import get_offers, offer_for, record_attempt, utcnow
from aivo.controller.endpoints import EndpointManager
from aivo.controller.jobspec import FRIENDLY_ERRORS, job_request, resolve
from aivo.controller.lifecycle import TERMINAL_JOB_STATES, transition_job
from aivo.db.models import Job
from aivo.providers.base import AuthError, CapacityError, ConfigError, GpuOffer, QuotaError
from aivo_protocol.worker_api import Ticket

log = logging.getLogger(__name__)

ACTIVE = ("assigned", "preparing", "running", "fetching")
# how long after RunPod says "completed" the worker's own report may still be on its way
COMPLETION_GRACE_S = 60


def ticket_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class JobRunner:
    def __init__(self, app: AppContext, *, poll_s: float = 5.0, endpoints: EndpointManager | None = None):
        self.app = app
        self.poll_s = poll_s
        self.endpoints = endpoints or EndpointManager(app)

    async def _load(self, job_id: uuid.UUID) -> Job | None:
        async with self.app.sessionmaker() as s:
            return (await s.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()

    async def run(self, job_id: uuid.UUID) -> None:
        completed_seen: float | None = None
        while True:
            job = await self._load(job_id)
            if job is None or job.state in TERMINAL_JOB_STATES or job.state not in ACTIVE:
                return
            try:
                if job.cancel_requested and job.state == "assigned":
                    await self._cancel(job)
                    return
                if job.runpod_job_id is None:
                    await self._submit(job)
                    continue
                st = await self.app.runpod.status(str(job.endpoint_runpod_id), job.runpod_job_id)
                completed_seen = await self._check(job, st, completed_seen)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("job %s: %s", job_id, e)
            await asyncio.sleep(self.poll_s)

    # -- submitting ------------------------------------------------------------------

    async def _submit(self, job: Job) -> None:
        app = self.app
        rs = await app.runtime()
        offers = await get_offers(app)
        offer = offer_for(offers, job.gpu_type or "")
        if offer is None:
            await self._next_gpu(job, f"{job.gpu_type} is no longer offered")
            return
        try:
            ep = await self.endpoints.ensure(offer)
        except (CapacityError, QuotaError) as e:
            await self._record(offer, "quota" if isinstance(e, QuotaError) else "capacity", str(e))
            await self._next_gpu(job, str(e), quota=isinstance(e, QuotaError))
            return
        except (AuthError, ConfigError) as e:
            await self._record(offer, "error", str(e))
            await self._finish(job.id, "failed", "config", f"RunPod is not set up correctly: {e}")
            return
        token = secrets.token_urlsafe(32)
        ticket = Ticket(aivo_url=app.worker_api_url, job_id=str(job.id), token=token)
        # the ticket is valid before RunPod has the job: a worker may start on it right away
        async with app.sessionmaker() as s, s.begin():
            ok = await transition_job(
                s,
                job.id,
                "assigned",
                "assigned",
                endpoint_id=ep.id,
                endpoint_runpod_id=ep.runpod_id,
                ticket_hash=ticket_hash(token),
                stage="waiting_for_gpu",
            )
        if not ok:
            return
        # the job's own limit (several runs for a creation with many videos), plus time to download models
        limit_s = int((job.resolved or {}).get("timeout_s") or rs.job_timeout_s)
        policy = {
            "executionTimeout": (limit_s + 1200) * 1000,
            "ttl": (rs.capacity_wait_max_s + limit_s + 1800) * 1000,
        }
        rp_id = await app.runpod.run(str(ep.runpod_id), ticket.model_dump(), policy)
        async with app.sessionmaker() as s, s.begin():
            row = (await s.execute(select(Job).where(Job.id == job.id).with_for_update())).scalar_one()
            row.runpod_job_id, row.submitted_at = rp_id, utcnow()
            await record_attempt(s, None, offer, "created")
        log.info("job %s queued on %s (%s)", job.id, offer.size_slug, rp_id)

    async def _record(self, offer: GpuOffer, outcome: str, error: str) -> None:
        async with self.app.sessionmaker() as s, s.begin():
            await record_attempt(s, None, offer, outcome, error=error)

    async def _next_gpu(self, job: Job, reason: str, *, quota: bool = False) -> None:
        """Move the job to the next GPU type on its list, or back to waiting for any GPU."""
        app = self.app
        rs = await app.runtime()
        cands = list(job.candidates or [])
        types = [c["offer"]["size_slug"] for c in cands]
        idx = types.index(job.gpu_type) if job.gpu_type in types else -1
        nxt = cands[idx + 1] if idx + 1 < len(cands) else None
        now = utcnow()
        async with app.sessionmaker() as s, s.begin():
            row = (await s.execute(select(Job).where(Job.id == job.id).with_for_update())).scalar_one()
            if row.state != "assigned":
                return
            if nxt is not None:
                fp, pl, _req = job_request(app, row, rs)
                resolved = await resolve(app, s, row, fp, pl, nxt["variant_id"], rs)
                await transition_job(
                    s,
                    row.id,
                    "assigned",
                    "assigned",
                    gpu_type=nxt["offer"]["size_slug"],
                    variant_id=nxt["variant_id"],
                    resolved=resolved,
                    runpod_job_id=None,
                    endpoint_runpod_id=None,
                    ticket_hash=None,
                    endpoint_id=None,
                    attempt=row.attempt + 1,
                    stage="waiting_for_gpu",
                    event_payload={"machine": nxt["offer"]["gpu_model"], "reason": reason[:200]},
                )
                return
            wait_until = row.capacity_wait_until or now + timedelta(seconds=rs.capacity_wait_max_s)
            if now >= wait_until:
                await transition_job(
                    s,
                    row.id,
                    "assigned",
                    "failed",
                    error_code="no_capacity",
                    stage="failed",
                    error_message="No GPU became available. Please try again later.",
                    reserved_usd=0,
                )
                return
            await transition_job(
                s,
                row.id,
                "assigned",
                "waiting_capacity",
                stage="waiting_for_gpu",
                runpod_job_id=None,
                endpoint_runpod_id=None,
                ticket_hash=None,
                endpoint_id=None,
                reserved_usd=0,
                next_attempt_at=min(now + timedelta(seconds=120 if quota else 60), wait_until),
                error_message="Your RunPod account's worker limit is reached." if quota else None,
            )

    # -- watching --------------------------------------------------------------------

    async def _check(self, job: Job, st: dict[str, Any] | None, completed_seen: float | None) -> float | None:
        rs = await self.app.runtime()
        now = utcnow()
        status = (st or {}).get("status")
        if st is None:
            await self._finish(job.id, "failed", "lost", "RunPod no longer knows this job.")
            return None
        if status == "IN_QUEUE":
            waited = (now - job.submitted_at).total_seconds() if job.submitted_at else 0
            if job.state == "assigned" and waited > rs.gpu_fallback_after_s:
                await self.app.runpod.cancel(str(job.endpoint_runpod_id), str(job.runpod_job_id))
                offers = await get_offers(self.app)
                offer = offer_for(offers, job.gpu_type or "")
                if offer:
                    await self._record(offer, "capacity", f"no worker took the job within {int(waited)} s")
                await self._next_gpu(job, "no GPU of this type became free")
            return None
        if status == "IN_PROGRESS":
            if job.cancel_requested and job.state in ("assigned",):
                await self._cancel(job)
            return None
        if status == "COMPLETED":
            # the worker reports to aivo before it returns; give a late report a moment
            t = asyncio.get_running_loop().time()
            if completed_seen is None:
                return t
            if t - completed_seen > COMPLETION_GRACE_S:
                await self._finish(job.id, "failed", "lost", "The worker finished without delivering the result.")
            return completed_seen
        if status in ("FAILED", "TIMED_OUT", "CANCELLED"):
            if job.cancel_requested or status == "CANCELLED":
                await self._finish(job.id, "cancelled", "cancelled", None)
            elif status == "TIMED_OUT":
                await self._finish(job.id, "failed", "timeout", FRIENDLY_ERRORS["timeout"])
            else:
                detail = str((st or {}).get("error") or "")[:500]
                log.warning("job %s failed at RunPod: %s", job.id, detail)
                await self._finish(
                    job.id, "failed", "lost", f"{FRIENDLY_ERRORS['lost']} ({detail})" if detail else None
                )
        return None

    async def _cancel(self, job: Job) -> None:
        if job.runpod_job_id and job.endpoint_runpod_id:
            try:
                await self.app.runpod.cancel(str(job.endpoint_runpod_id), job.runpod_job_id)
            except Exception as e:
                log.info("cancel at RunPod failed: %s", e)
        await self._finish(job.id, "cancelled", "cancelled", None)

    async def _finish(self, job_id: uuid.UUID, state: str, code: str, message: str | None) -> None:
        from aivo.api.routes.worker import discard_outputs, finish_partly

        async with self.app.sessionmaker() as s, s.begin():
            row = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
            if row is None or row.state in TERMINAL_JOB_STATES:
                return
            if await finish_partly(s, row, "cancelled" if state == "cancelled" else code):
                return  # some results had arrived: they're kept
            await transition_job(
                s,
                job_id,
                row.state,
                state,
                stage=state,
                error_code=None if state == "cancelled" else code,
                error_message=None if state == "cancelled" else (message or FRIENDLY_ERRORS.get(code)),
                ticket_hash=None,
                reserved_usd=0,
            )
        await discard_outputs(self.app, job_id)
