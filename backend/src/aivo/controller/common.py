"""Shared helpers for the controller: GPU offers, placement context, telemetry."""

from __future__ import annotations

import itertools
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.config import RuntimeSettings
from aivo.context import AppContext
from aivo.db.models import Estimate, Instance, Job, PhaseTiming, PlacementAttempt
from aivo.placement.estimates import Estimator
from aivo.placement.scorer import Context
from aivo.providers.base import GpuOffer

FAILURE_OUTCOMES = ("capacity", "quota", "late_failure", "boot_failure", "error")


def utcnow() -> datetime:
    return datetime.now(UTC)


async def get_offers(app: AppContext, *, max_age_s: float = 60, force: bool = False) -> list[GpuOffer]:
    oc = app.offers
    if force or not oc.offers or time.monotonic() - oc.fetched_at > max_age_s:
        try:
            offers = await app.gpus.offers()
            for i, o in enumerate(offers):
                if o.gpu_class is None:
                    gc = app.catalog.gpu_class_for(o.gpu_model, o.size_slug)
                    if gc:
                        offers[i] = GpuOffer(**{**o.__dict__, "gpu_class": gc})
            oc.offers, oc.fetched_at, oc.error = offers, time.monotonic(), None
        except Exception as e:  # keep serving the last known list
            oc.error = str(e)
            if not oc.offers:
                raise
    return oc.offers


def offer_for(offers: list[GpuOffer], gpu_type: str) -> GpuOffer | None:
    return next((o for o in offers if o.size_slug == gpu_type), None)


async def build_context(app: AppContext, session: AsyncSession, rs: RuntimeSettings) -> Context:
    now = utcnow()
    attempts = (
        await session.execute(
            select(
                PlacementAttempt.size_slug, PlacementAttempt.region, PlacementAttempt.outcome, PlacementAttempt.at
            ).where(PlacementAttempt.at > now - timedelta(hours=48))
        )
    ).all()
    fails: dict[tuple[str, str], float] = {}
    totals: dict[tuple[str, str], float] = {}
    for size, region, outcome, at in attempts:
        age_h = (now - at).total_seconds() / 3600
        w = 0.5 ** (age_h / 6)
        key = (size, region)
        totals[key] = totals.get(key, 0) + w
        if outcome in FAILURE_OUTCOMES:
            fails[key] = fails.get(key, 0) + w
    p_fail = {k: (fails.get(k, 0) + 1) / (totals[k] + 5) for k in totals}
    est_values = {e.key: e.ewma for e in (await session.execute(select(Estimate))).scalars().all()}
    # GPU types with a worker up right now (it may still be busy, but takes the next job without a cold start)
    warm = (
        await session.execute(
            select(Instance.size_slug).where(Instance.provider == "runpod", Instance.state.in_(("ready", "busy")))
        )
    ).scalars()
    return Context(
        preference=rs.preference_default,
        denied_sizes=frozenset(rs.denied_sizes),
        max_price_hourly=rs.max_price_hourly,
        idle_timeout_s=rs.keep_warm_s,
        min_billing_s=0,
        create_unbilled=True,
        tail_share=await tail_share(session, rs),
        allow_new=True,
        estimator=Estimator(est_values),
        gpu_perf={g.id: g.perf for g in app.catalog.gpu_classes.values()},
        p_fail=p_fail,
        warm_sizes=frozenset(warm),
    )


async def tail_share(session: AsyncSession, rs: RuntimeSettings) -> float:
    """1 - P(another job arrives before an idle worker scales down), from the last 30 days."""
    rows = (
        (
            await session.execute(
                select(Job.created_at).where(Job.created_at > utcnow() - timedelta(days=30)).order_by(Job.created_at)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) < 5:
        return 0.5
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(rows)]
    followups = sum(1 for g in gaps if g < rs.keep_warm_s)
    return max(0.1, 1 - followups / len(gaps))


async def record_attempt(
    session: AsyncSession,
    inst_id: uuid.UUID | None,
    offer: GpuOffer,
    outcome: str,
    *,
    rank: int = 0,
    http_status: int | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    session.add(
        PlacementAttempt(
            instance_id=inst_id,
            rank=rank,
            provider=offer.provider,
            size_slug=offer.size_slug,
            region=offer.region,
            outcome=outcome,
            http_status=http_status,
            error_text=(error or "")[:2000] or None,
            duration_ms=duration_ms,
        )
    )


async def record_phase(
    session: AsyncSession,
    *,
    phase: str,
    seconds: float,
    inst: Instance | None = None,
    job: Job | None = None,
    source: str | None = None,
    bytes_: int | None = None,
    estimate_key: str | None = None,
    units: float | None = None,
    quality: str | None = None,
) -> None:
    session.add(
        PhaseTiming(
            instance_id=inst.id if inst else None,
            job_id=job.id if job else None,
            phase=phase,
            source=source,
            size_slug=inst.size_slug if inst else (job.gpu_type if job else None),
            gpu_class=inst.gpu_class if inst else None,
            region=inst.region if inst else None,
            model_id=job.model_id if job else None,
            variant_id=job.variant_id if job else None,
            quality=quality,
            units=units,
            bytes=bytes_,
            seconds=seconds,
        )
    )
    if estimate_key:
        await update_estimate(session, estimate_key, seconds)


async def update_estimate(session: AsyncSession, key: str, value: float) -> None:
    """EWMA update for the hierarchical estimate keys (most specific + parents)."""
    parts = key.split(":")
    keys = [":".join(parts[:n]) for n in range(len(parts), 1, -1)]
    for k in keys:
        await session.execute(
            insert(Estimate)
            .values(key=k, ewma=value, n=1)
            .on_conflict_do_update(
                index_elements=[Estimate.key],
                set_={"ewma": Estimate.ewma * 0.7 + value * 0.3, "n": Estimate.n + 1, "updated_at": func.now()},
            )
        )
