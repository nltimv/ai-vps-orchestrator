"""Periodic reconciliation between the database and the cloud provider.

Every tick:
* sync machines tagged with this deployment; machines that vanished are marked
  destroyed and their jobs re-queued; unknown tagged machines (orphans) older
  than a few minutes are destroyed. Machines of *other* deployments are never touched.
* idle machines past the idle timeout (and not "kept warm") are drained and destroyed
* maximum lifetime and stuck start-ups are enforced
* cost is accrued and the monthly budget enforced
* ``destroying`` machines are destroyed (idempotently) until the provider confirms
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.context import AppContext
from aivo.controller.budget import accrue, admit, ensure_period, month_compute_spend
from aivo.controller.common import utcnow, worker_client
from aivo.controller.lifecycle import STARTING_STATES, transition_instance, transition_job
from aivo.db.models import CacheResource, Event, Instance, Job
from aivo.events.bus import emit
from aivo.providers.base import ProviderInstance, deployment_tag
from aivo.worker_client.client import WorkerUnreachable

log = logging.getLogger(__name__)

ORPHAN_MIN_AGE_S = 180
VANISH_GRACE_S = 90


class Reconciler:
    def __init__(
        self,
        app: AppContext,
        *,
        fence: Callable[[AsyncSession], object] | None = None,
        orphan_min_age_s: float = ORPHAN_MIN_AGE_S,
        vanish_grace_s: float = VANISH_GRACE_S,
    ):
        self.app = app
        self.fence = fence
        self.orphan_min_age_s = orphan_min_age_s
        self.vanish_grace_s = vanish_grace_s
        self._missing: dict[uuid.UUID, int] = {}
        self._health_fail: dict[uuid.UUID, int] = {}
        self._ticks = 0

    async def _fenced(self, s: AsyncSession) -> bool:
        if self.fence is None:
            return True
        return bool(await self.fence(s))  # type: ignore[misc]

    async def tick(self) -> None:
        self._ticks += 1
        app = self.app
        rs = await app.runtime(fresh=True)
        now = utcnow()
        try:
            remote = await app.provider.list_instances(deployment_tag(app.deployment_id))
        except Exception as e:
            log.warning("reconciler: provider list failed: %s", e)
            remote = None
        if remote is not None:
            await self._sync(remote)
        await self._idle_and_lifetime(rs)
        await self._stuck(rs)
        await self._drain()
        await self._destroy()
        await self._budget(rs)
        if self._ticks % 3 == 0:
            await self._health_ready()
        if self._ticks % 360 == 0:
            async with app.sessionmaker() as s, s.begin():
                await s.execute(delete(Event).where(Event.at < now - timedelta(days=7)))

    # ------------------------------------------------------------------ provider sync

    async def _sync(self, remote: list[ProviderInstance]) -> None:
        app = self.app
        now = utcnow()
        by_pid = {r.provider_id: r for r in remote}
        async with app.sessionmaker() as s, s.begin():
            rows = (
                await s.execute(select(Instance).where(Instance.state != "destroyed"))
            ).scalars().all()
            known_pids = {r.provider_instance_id for r in rows if r.provider_instance_id}
            known_ids = {str(r.id) for r in rows}
            for inst in rows:
                if not inst.provider_instance_id or inst.state in ("pending", "creating"):
                    continue
                if inst.provider_instance_id in by_pid:
                    self._missing.pop(inst.id, None)
                    continue
                age = (now - (inst.create_accepted_at or inst.created_at)).total_seconds()
                if age < self.vanish_grace_s:
                    continue  # provider lists can lag right after create
                self._missing[inst.id] = self._missing.get(inst.id, 0) + 1
                if self._missing[inst.id] >= 2:
                    self._missing.pop(inst.id, None)
                    await self._mark_gone(s, inst, external=inst.state != "destroying")
            # orphans: tagged with our deployment but not (or no longer) tracked
            for r in remote:
                if r.provider_id in known_pids:
                    continue
                inst_uuid = next((t[len("aivo-inst-"):] for t in r.tags if t.startswith("aivo-inst-")), None)
                if inst_uuid and inst_uuid in known_ids:
                    continue  # create in flight; the provisioner will adopt it
                age = (now - r.created_at).total_seconds() if r.created_at else 1e9
                if age < self.orphan_min_age_s:
                    continue
                if not await self._fenced(s):
                    return
                log.warning("destroying orphan machine %s (%s)", r.provider_id, r.name)
                try:
                    await app.provider.destroy_instance(r.provider_id)
                    await emit(s, scope="system", type="orphan.destroyed",
                               payload={"provider_id": r.provider_id, "name": r.name})
                except Exception as e:
                    log.warning("orphan destroy failed: %s", e)

    async def _mark_gone(self, s: AsyncSession, inst: Instance, *, external: bool) -> None:
        await s.execute(
            update(Instance).where(Instance.id == inst.id).values(
                state="destroyed", destroy_confirmed_at=func.now(), state_changed_at=func.now(),
                outcome="external" if external else inst.outcome or "ok",
                failure_reason="machine disappeared" if external else inst.failure_reason,
            )
        )
        await emit(s, scope="instance", type="instance.state", ref_id=inst.id,
                   payload={"state": "destroyed", "external": external})
        await self._release_resources(s, inst.id)
        await self._requeue_jobs(s, inst.id)

    async def _requeue_jobs(self, s: AsyncSession, inst_id: uuid.UUID) -> None:
        jobs = (
            await s.execute(
                select(Job).where(
                    Job.instance_id == inst_id,
                    Job.state.in_(("provisioning", "queued", "assigned", "preparing", "running", "fetching")),
                )
            )
        ).scalars().all()
        for j in jobs:
            started = j.state in ("preparing", "running", "fetching")
            if started and j.attempt + 1 >= j.max_attempts:
                await transition_job(s, j.id, j.state, "failed", error_code="lost", stage="failed",
                                     error_message="The machine stopped responding.", instance_id=None)
            else:
                await transition_job(s, j.id, j.state, "queued", instance_id=None, stage="queued",
                                     attempt=j.attempt + (1 if started else 0), progress=0.0)

    async def _release_resources(self, s: AsyncSession, inst_id: uuid.UUID) -> None:
        await s.execute(
            update(CacheResource).where(CacheResource.attached_instance_id == inst_id).values(attached_instance_id=None)
        )

    # ------------------------------------------------------------------ idle / lifetime

    async def _idle_and_lifetime(self, rs) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        async with self.app.sessionmaker() as s, s.begin():
            insts = (
                await s.execute(select(Instance).where(Instance.state.in_(("ready", "busy"))))
            ).scalars().all()
            for inst in insts:
                reserved = (
                    await s.execute(
                        select(func.count()).select_from(Job).where(
                            Job.instance_id == inst.id, Job.state.in_(("provisioning", "queued", "assigned")),
                        )
                    )
                ).scalar_one()
                hard_deadline = (
                    inst.max_lifetime_at + timedelta(seconds=rs.lifetime_grace_s) if inst.max_lifetime_at else None
                )
                if hard_deadline and now > hard_deadline:
                    await transition_instance(s, inst.id, ("ready", "busy"), "destroying",
                                              drain_reason="max_lifetime", destroy_requested_at=now)
                    await self._requeue_jobs(s, inst.id)
                    continue
                if inst.state != "ready" or reserved:
                    continue
                keep_warm = inst.keep_warm_until is not None and inst.keep_warm_until > now
                idle_for = (now - inst.idle_since).total_seconds() if inst.idle_since else 0
                # you pay the provider's minimum anyway, so never destroy before it has elapsed
                billed_for = (now - inst.create_accepted_at).total_seconds() if inst.create_accepted_at else 0
                timeout = max(rs.idle_timeout_s, 0)
                reason = None
                if inst.max_lifetime_at and now > inst.max_lifetime_at:
                    reason = "max_lifetime"
                elif inst.drain_reason in ("budget", "lifetime", "manual"):
                    reason = inst.drain_reason
                elif not keep_warm and idle_for >= timeout and billed_for >= rs.min_billing_s:
                    reason = "idle"
                if reason:
                    await transition_instance(s, inst.id, "ready", "draining", draining_at=now, drain_reason=reason,
                                              event_payload={"reason": reason})

    async def _stuck(self, rs) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        controller = self.app.controller
        async with self.app.sessionmaker() as s, s.begin():
            rows = (
                await s.execute(select(Instance).where(Instance.state.in_(STARTING_STATES)))
            ).scalars().all()
            for inst in rows:
                age = (now - inst.state_changed_at).total_seconds()
                limit = {
                    "pending": rs.provision_deadline_s, "creating": rs.provision_deadline_s,
                    "provisioning": rs.boot_ip_timeout_s, "booting": rs.boot_health_timeout_s,
                }[inst.state] + 60
                running = controller is not None and controller.has_provisioner(inst.id)
                if age > limit and not running:
                    if inst.provider_instance_id:
                        await transition_instance(s, inst.id, inst.state, "destroying", outcome="failed",
                                                  failure_reason=f"stuck in {inst.state}", destroy_requested_at=now)
                    else:
                        await transition_instance(s, inst.id, inst.state, "destroyed", outcome="failed",
                                                  failure_reason=f"stuck in {inst.state}")
                    await self._requeue_jobs(s, inst.id)

    async def _drain(self) -> None:
        async with self.app.sessionmaker() as s:
            rows = (await s.execute(select(Instance).where(Instance.state == "draining"))).scalars().all()
        for inst in rows:
            busy = False
            try:
                async with worker_client(self.app, inst) as c:
                    h = await c.drain()
                    busy = bool(h.running_job) or h.outputs_pending > 0
            except Exception:
                busy = False  # unreachable -> nothing to wait for
            async with self.app.sessionmaker() as s, s.begin():
                active = (
                    await s.execute(
                        select(func.count()).select_from(Job).where(
                            Job.instance_id == inst.id, Job.state.in_(("assigned", "preparing", "running", "fetching"))
                        )
                    )
                ).scalar_one()
                if busy and active:
                    continue
                await transition_instance(s, inst.id, "draining", "destroying", destroy_requested_at=utcnow(),
                                          outcome=inst.outcome or "ok")
                await self._requeue_jobs(s, inst.id)

    async def _destroy(self) -> None:
        async with self.app.sessionmaker() as s:
            rows = (await s.execute(select(Instance).where(Instance.state == "destroying"))).scalars().all()
        for inst in rows:
            async with self.app.sessionmaker() as s, s.begin():
                if not await self._fenced(s):
                    return
                try:
                    if inst.provider_instance_id:
                        await self.app.provider.destroy_instance(inst.provider_instance_id)
                        gone = await self.app.provider.get_instance(inst.provider_instance_id) is None
                    else:
                        gone = True
                except Exception as e:
                    log.warning("destroy %s failed (will retry): %s", inst.id, e)
                    continue
                if gone:
                    await transition_instance(s, inst.id, "destroying", "destroyed", destroy_confirmed_at=utcnow())
                    await self._release_resources(s, inst.id)
                    await self._requeue_jobs(s, inst.id)
                    self._health_fail.pop(inst.id, None)

    async def _health_ready(self) -> None:
        async with self.app.sessionmaker() as s:
            rows = (await s.execute(select(Instance).where(Instance.state == "ready"))).scalars().all()
        for inst in rows:
            try:
                async with worker_client(self.app, inst) as c:
                    h = await c.health()
                self._health_fail.pop(inst.id, None)
                async with self.app.sessionmaker() as s, s.begin():
                    await s.execute(update(Instance).where(Instance.id == inst.id).values(
                        last_contact_at=utcnow(), files_present=h.files_present, models_loaded=h.models_loaded))
            except (WorkerUnreachable, Exception):
                n = self._health_fail.get(inst.id, 0) + 1
                self._health_fail[inst.id] = n
                if n >= 3:
                    async with self.app.sessionmaker() as s, s.begin():
                        await transition_instance(s, inst.id, "ready", "destroying", outcome="failed",
                                                  failure_reason="stopped responding", destroy_requested_at=utcnow())
                        await self._requeue_jobs(s, inst.id)

    # ------------------------------------------------------------------ budget

    async def _budget(self, rs) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        async with self.app.sessionmaker() as s, s.begin():
            await accrue(s, rs, now)
            period = await ensure_period(s, rs, now, lock=True)
            spent = await month_compute_spend(s, period.period)
            hard = Decimal(str(rs.hard_cap_usd))
            if spent >= hard and not period.locked:
                period.locked = True
                await emit(s, scope="system", type="budget.locked", payload={"spent": float(spent)})
            elif spent < hard and period.locked:
                period.locked = False  # cap was raised
            if period.locked:
                insts = (await s.execute(select(Instance).where(Instance.state.in_(("ready", "busy"))))).scalars().all()
                for inst in insts:
                    if spent >= hard + Decimal(str(rs.overrun_margin_usd)):
                        await transition_instance(s, inst.id, ("ready", "busy"), "destroying",
                                                  drain_reason="budget", destroy_requested_at=now)
                        await self._requeue_jobs(s, inst.id)
                    elif inst.state == "ready":
                        await transition_instance(s, inst.id, "ready", "draining", drain_reason="budget",
                                                  draining_at=now)
                    else:
                        inst.drain_reason = "budget"
                return
            # extend reservations of busy machines that run longer than expected
            busy = (await s.execute(select(Instance).where(Instance.state == "busy"))).scalars().all()
            for inst in busy:
                if inst.create_accepted_at is None:
                    continue
                running_usd = Decimal(str(float(inst.price_hourly) * (now - inst.create_accepted_at).total_seconds() / 3600))
                if running_usd >= inst.reserved_usd * Decimal("0.9"):
                    extra = Decimal(str(round(float(inst.price_hourly) * 0.5, 4)))
                    adm = await admit(s, rs, now, extra)
                    if adm.ok:
                        inst.reserved_usd = inst.reserved_usd + extra
                    else:
                        inst.drain_reason = "budget"
