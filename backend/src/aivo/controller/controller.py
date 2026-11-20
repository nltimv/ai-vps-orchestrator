"""Controller supervisor: holds the lease and runs scheduler/reconciler loops plus
one task per provisioning machine and per running job."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from aivo.context import AppContext
from aivo.controller.common import get_offers
from aivo.controller.dispatcher import JobRunner
from aivo.controller.lease import LeaseManager
from aivo.controller.lifecycle import ACTIVE_JOB_STATES, STARTING_STATES
from aivo.controller.provisioner import Provisioner
from aivo.controller.reconciler import Reconciler
from aivo.controller.scheduler import Scheduler
from aivo.db.models import Instance, Job

log = logging.getLogger(__name__)


class Controller:
    def __init__(
        self,
        app: AppContext,
        *,
        scheduler_interval_s: float = 3.0,
        reconcile_interval_s: float = 10.0,
        lease_ttl_s: int = 30,
        provisioner: Provisioner | None = None,
        runner: JobRunner | None = None,
        reconciler: Reconciler | None = None,
    ):
        self.app = app
        self.scheduler_interval_s = scheduler_interval_s
        self.reconcile_interval_s = reconcile_interval_s
        self.lease = LeaseManager(app.sessionmaker, ttl_s=lease_ttl_s)
        self.scheduler = Scheduler(app)
        self.provisioner = provisioner or Provisioner(app)
        self.runner = runner or JobRunner(app)
        self.reconciler = reconciler or Reconciler(app, fence=self.lease.still_valid)
        self._wake = asyncio.Event()
        self._provisioning: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._jobs: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._loops: list[asyncio.Task[None]] = []
        self._main: asyncio.Task[None] | None = None
        self.is_leader = asyncio.Event()
        self._stopping = False
        app.controller = self

    # ------------------------------------------------------------------ public

    def wake(self) -> None:
        self._wake.set()

    def has_provisioner(self, instance_id: uuid.UUID) -> bool:
        t = self._provisioning.get(instance_id)
        return t is not None and not t.done()

    async def start(self) -> None:
        self._main = asyncio.create_task(self._supervise(), name="controller")

    async def stop(self) -> None:
        self._stopping = True
        await self._stop_loops()
        if self._main:
            self._main.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._main
        with contextlib.suppress(Exception):
            await self.lease.release()

    # ------------------------------------------------------------------ supervision

    async def _supervise(self) -> None:
        while not self._stopping:
            try:
                if await self.lease.try_acquire():
                    log.info("controller lease acquired (epoch %s)", self.lease.epoch)
                    self.is_leader.set()
                    self._loops = [
                        asyncio.create_task(self._loop("scheduler", self._schedule_once, self.scheduler_interval_s, wake=True)),
                        asyncio.create_task(self._loop("reconciler", self._reconcile_once, self.reconcile_interval_s)),
                        asyncio.create_task(self._loop("offers", self._refresh_offers, 60)),
                    ]
                    await self._resume()
                    while not self._stopping:
                        await asyncio.sleep(self.lease.ttl_s / 3)
                        if not await self.lease.renew():
                            log.error("controller lease lost; stopping all controller work")
                            break
                    self.is_leader.clear()
                    await self._stop_loops()
                else:
                    await asyncio.sleep(self.lease.ttl_s / 3)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("controller supervisor error")
                self.is_leader.clear()
                await self._stop_loops()
                await asyncio.sleep(5)

    async def _stop_loops(self) -> None:
        tasks = [*self._loops, *self._provisioning.values(), *self._jobs.values()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._loops, self._provisioning, self._jobs = [], {}, {}

    async def _loop(self, name: str, fn: Callable[[], Awaitable[None]], interval: float, wake: bool = False) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s iteration failed", name)
            if wake:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=interval)
                self._wake.clear()
            else:
                await asyncio.sleep(interval)

    async def _schedule_once(self) -> None:
        to_provision, to_run = await self.scheduler.plan_once()
        for iid in to_provision:
            self.spawn_provisioner(iid)
        for jid in to_run:
            self.spawn_job(jid)

    async def _reconcile_once(self) -> None:
        await self.reconciler.tick()
        await self._resume()

    async def _refresh_offers(self) -> None:
        await get_offers(self.app, force=True)

    async def _resume(self) -> None:
        """(Re)start tasks for work that is in flight according to the database."""
        async with self.app.sessionmaker() as s:
            starting = (await s.execute(select(Instance.id).where(Instance.state.in_(STARTING_STATES)))).scalars().all()
            active = (await s.execute(select(Job.id).where(Job.state.in_(ACTIVE_JOB_STATES)))).scalars().all()
        for iid in starting:
            self.spawn_provisioner(iid)
        for jid in active:
            self.spawn_job(jid)

    def spawn_provisioner(self, iid: uuid.UUID) -> None:
        if self.has_provisioner(iid):
            return
        self._provisioning[iid] = asyncio.create_task(self._wrap(self.provisioner.run(iid)), name=f"provision:{iid}")

    def spawn_job(self, jid: uuid.UUID) -> None:
        t = self._jobs.get(jid)
        if t is not None and not t.done():
            return
        self._jobs[jid] = asyncio.create_task(self._wrap(self.runner.run(jid)), name=f"job:{jid}")

    async def _wrap(self, coro: Awaitable[None]) -> None:
        try:
            await coro
        finally:
            self.wake()
