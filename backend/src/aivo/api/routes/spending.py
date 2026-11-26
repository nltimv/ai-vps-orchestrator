from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.api.deps import current_user, get_app, get_session
from aivo.api.schemas import SpendingOut
from aivo.context import AppContext
from aivo.controller.budget import (
    ensure_period,
    month_compute_spend,
    open_reservations,
    period_of,
)
from aivo.db.models import CostAccrual, Instance, Job, User
from aivo.providers.regions import region_meta
from aivo.safety import visible

router = APIRouter(tags=["spending"])


@router.get("/spending")
async def spending(
    user: User = Depends(current_user), app: AppContext = Depends(get_app), s: AsyncSession = Depends(get_session)
) -> SpendingOut:
    rs = await app.runtime(s)
    now = datetime.now(UTC)
    period = await ensure_period(s, rs, now, lock=False)
    compute = await month_compute_spend(s, period.period)
    reserved = await open_reservations(s)
    storage = 0.0
    running = (
        await s.execute(select(func.count()).select_from(Instance).where(Instance.state.in_(("ready", "busy"))))
    ).scalar_one()
    provider_mtd = None
    by_machine_rows = (
        await s.execute(
            select(Instance, CostAccrual.usd, CostAccrual.billed_seconds)
            .join(CostAccrual, CostAccrual.instance_id == Instance.id)
            .where(CostAccrual.period == period_of(now))
            .order_by(Instance.created_at.desc())
            .limit(50)
        )
    ).all()
    by_day: dict[str, float] = {}
    for inst, usd, _secs in by_machine_rows:
        d = (inst.create_accepted_at or inst.created_at).date().isoformat()
        by_day[d] = by_day.get(d, 0.0) + float(usd)
    # the month's totals are everyone's (one budget); the list of creations is your own, or everyone's for
    # admins (who, which engine, what it cost: never what it shows)
    q = (
        select(Job, User.display_name)
        .join(User, User.id == Job.user_id)
        .where(Job.created_at >= datetime(now.year, now.month, 1, tzinfo=UTC))
    )
    if user.role != "admin":
        q = q.where(Job.user_id == user.id)
    jobs = (await s.execute(visible(q, user, rs).order_by(Job.created_at.desc()).limit(50))).all()
    recent = [
        {
            "id": str(j.id),
            "kind": j.kind,
            "model": app.catalog.models[j.model_id].manifest.display_name
            if j.model_id in app.catalog.models
            else j.model_id,
            "user": name,
            "state": j.state,
            "created_at": j.created_at.isoformat(),
            "est_cost_usd": j.est_cost_usd,
            "cost_usd": j.attributed_cost_usd,
        }
        for j, name in jobs
    ]
    await s.commit()
    return SpendingOut(
        period=period.period.isoformat(),
        compute_usd=float(compute),
        reserved_usd=float(reserved),
        storage_monthly_usd=storage,
        soft_cap_usd=rs.soft_cap_usd,
        hard_cap_usd=rs.hard_cap_usd,
        locked=period.locked,
        provider_month_to_date_usd=provider_mtd,
        running_machines=running,
        by_day=[{"day": d, "usd": round(v, 4)} for d, v in sorted(by_day.items())],
        recent_jobs=recent,
        by_machine=[
            {
                "id": str(i.id),
                "gpu": i.gpu_model,
                "region": region_meta(i.region).name,
                "state": i.state,
                "usd": float(usd),
                "hours": round(secs / 3600, 3),
                "created_at": i.created_at.isoformat(),
            }
            for i, usd, secs in by_machine_rows
        ],
    )
