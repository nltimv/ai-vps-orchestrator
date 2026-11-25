"""Periodic upkeep (every ~10 s, on the controller leader only).

* budget: accrue worker time, lock the month at the hard cap, cancel running jobs beyond the overrun margin
* workers: close billing records of workers RunPod has scaled down by now
* endpoints: keep them in step with settings and releases, delete ones aivo no longer knows (every minute)
* events: drop entries older than a week (every hour)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.context import AppContext
from aivo.controller.budget import accrue, ensure_period, month_compute_spend
from aivo.controller.common import utcnow
from aivo.controller.endpoints import EndpointManager
from aivo.controller.workers import close_finished_workers
from aivo.db.models import Event, Job
from aivo.events.bus import emit

log = logging.getLogger(__name__)

ACTIVE = ("assigned", "preparing", "running", "fetching")


class Housekeeping:
    def __init__(
        self,
        app: AppContext,
        *,
        fence: Callable[[AsyncSession], Awaitable[bool]] | None = None,
        endpoints: EndpointManager | None = None,
        endpoint_sync_every: int = 6,
    ):
        self.app = app
        self.fence = fence
        self.endpoints = endpoints or EndpointManager(app)
        self.endpoint_sync_every = endpoint_sync_every
        self._ticks = 0

    async def tick(self) -> None:
        self._ticks += 1
        app = self.app
        rs = await app.runtime(fresh=True)
        now = utcnow()
        async with app.sessionmaker() as s, s.begin():
            await close_finished_workers(s, rs)
        await self._budget()
        if self._ticks % self.endpoint_sync_every == 1:
            try:
                await self.endpoints.sync()
            except Exception as e:
                log.warning("endpoint sync failed: %s", e)
        if self._ticks % 360 == 0:
            async with app.sessionmaker() as s, s.begin():
                await s.execute(delete(Event).where(Event.at < now - timedelta(days=7)))

    async def _budget(self) -> None:
        rs = await self.app.runtime()
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
                period.locked = False  # the cap was raised
            if period.locked and spent >= hard + Decimal(str(rs.overrun_margin_usd)):
                await s.execute(
                    update(Job)
                    .where(Job.state.in_(ACTIVE), Job.cancel_requested.is_(False))
                    .values(cancel_requested=True)
                )
