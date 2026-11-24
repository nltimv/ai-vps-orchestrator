"""Spend tracking and budget admission.

* Accrual: every machine is billed from "create accepted" to "destroy confirmed"
  (or now), with the provider's minimum billing applied, split per calendar month.
* Admission: inside the transaction that creates a machine, the month's row in
  ``spend_periods`` is locked ``FOR UPDATE``; a machine is admitted only if
  ``accrued + open reservations + its own reservation <= hard cap``. Two
  concurrent requests therefore cannot both slip under the cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.config import RuntimeSettings
from aivo.db.models import CacheResource, CostAccrual, Instance, SpendPeriod
from aivo.providers.base import traits_for

D = Decimal


def period_of(dt: datetime) -> date:
    return date(dt.year, dt.month, 1)


def _next_month(d: date) -> date:
    return date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)


def split_by_month(start: datetime, end: datetime) -> dict[date, float]:
    """Seconds of [start, end) falling into each calendar month (UTC)."""
    out: dict[date, float] = {}
    cur = start
    while cur < end:
        p = period_of(cur)
        boundary = datetime(_next_month(p).year, _next_month(p).month, 1, tzinfo=UTC)
        seg_end = min(end, boundary)
        out[p] = out.get(p, 0.0) + (seg_end - cur).total_seconds()
        cur = seg_end
    return out


def billed_window(inst: Instance, now: datetime, min_billing_s: float) -> tuple[datetime, datetime] | None:
    traits = traits_for(inst.provider)
    if traits.min_billing_s is not None:
        min_billing_s = traits.min_billing_s
    if inst.create_accepted_at is None:
        return None
    start = inst.create_accepted_at
    if traits.bills_from_boot:  # RunPod Serverless: preparing the host is free; the worker's run is billed
        if inst.ip_at is None:
            return None
        start = inst.ip_at
    end = inst.destroy_confirmed_at or now
    if end < start:
        end = start
    if (end - start).total_seconds() < min_billing_s and inst.destroy_confirmed_at is not None:
        end = start + timedelta(seconds=min_billing_s)
    return start, end


async def ensure_period(session: AsyncSession, rs: RuntimeSettings, when: datetime, *, lock: bool) -> SpendPeriod:
    p = period_of(when)
    await session.execute(
        insert(SpendPeriod)
        .values(period=p, soft_cap_usd=D(str(rs.soft_cap_usd)), hard_cap_usd=D(str(rs.hard_cap_usd)), locked=False)
        .on_conflict_do_nothing()
    )
    q = select(SpendPeriod).where(SpendPeriod.period == p)
    if lock:
        q = q.with_for_update()
    row = (await session.execute(q)).scalar_one()
    # keep caps in sync with settings
    row.soft_cap_usd = D(str(rs.soft_cap_usd))
    row.hard_cap_usd = D(str(rs.hard_cap_usd))
    return row


async def accrue(session: AsyncSession, rs: RuntimeSettings, now: datetime) -> None:
    since = now - timedelta(days=40)
    insts = (
        (
            await session.execute(
                select(Instance).where(
                    Instance.create_accepted_at.is_not(None),
                    (Instance.destroy_confirmed_at.is_(None)) | (Instance.destroy_confirmed_at > since),
                )
            )
        )
        .scalars()
        .all()
    )
    for inst in insts:
        win = billed_window(inst, now, rs.min_billing_s)
        if win is None:
            continue
        price = float(inst.price_hourly)
        for period, secs in split_by_month(*win).items():
            usd = D(str(round(secs * price / 3600, 4)))
            await session.execute(
                insert(CostAccrual)
                .values(instance_id=inst.id, period=period, billed_seconds=secs, usd=usd)
                .on_conflict_do_update(
                    index_elements=[CostAccrual.instance_id, CostAccrual.period],
                    set_={"billed_seconds": secs, "usd": usd, "updated_at": func.now()},
                )
            )


async def month_compute_spend(session: AsyncSession, period: date) -> Decimal:
    v = (
        await session.execute(select(func.coalesce(func.sum(CostAccrual.usd), 0)).where(CostAccrual.period == period))
    ).scalar_one()
    return D(v)


async def open_reservations(session: AsyncSession) -> Decimal:
    rows = (
        await session.execute(
            select(Instance.id, Instance.reserved_usd, func.coalesce(func.sum(CostAccrual.usd), 0))
            .outerjoin(CostAccrual, CostAccrual.instance_id == Instance.id)
            .where(Instance.state != "destroyed")
            .group_by(Instance.id, Instance.reserved_usd)
        )
    ).all()
    return sum((max(D(0), D(r[1]) - D(r[2])) for r in rows), D(0))


async def storage_monthly_estimate(session: AsyncSession) -> float:
    v = (
        await session.execute(
            select(func.coalesce(func.sum(CacheResource.monthly_cost_est), 0)).where(
                CacheResource.status.notin_(("deleted", "failed"))
            )
        )
    ).scalar_one()
    return float(v)


@dataclass
class Admission:
    ok: bool
    reason: str | None
    spent: Decimal
    reserved: Decimal
    hard_cap: Decimal
    soft_cap: Decimal

    @property
    def over_soft(self) -> bool:
        return self.spent + self.reserved >= self.soft_cap


async def admit(session: AsyncSession, rs: RuntimeSettings, now: datetime, amount: Decimal) -> Admission:
    """Must run inside the transaction that creates the reservation."""
    period = await ensure_period(session, rs, now, lock=True)
    spent = await month_compute_spend(session, period.period)
    reserved = await open_reservations(session)
    hard = D(str(rs.hard_cap_usd))
    soft = D(str(rs.soft_cap_usd))
    if period.locked:
        return Admission(False, "budget_locked", spent, reserved, hard, soft)
    if spent + reserved + amount > hard:
        return Admission(False, "over_hard_cap", spent, reserved, hard, soft)
    return Admission(True, None, spent, reserved, hard, soft)


def reservation_for(
    price_hourly: float, expected_billable_s: float, rs: RuntimeSettings, provider: str | None = None
) -> Decimal:
    traits = traits_for(provider)
    min_billing = rs.min_billing_s if traits.min_billing_s is None else traits.min_billing_s
    idle = rs.idle_timeout_s if traits.max_idle_s is None else min(rs.idle_timeout_s, traits.max_idle_s)
    secs = max(expected_billable_s, min_billing) + idle + 300
    return D(str(round(price_hourly * secs / 3600, 4)))
