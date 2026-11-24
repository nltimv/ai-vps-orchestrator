"""Shared helpers for the controller: placement context, offers cache, worker clients, telemetry."""

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
from aivo.db.models import (
    CacheResource,
    Estimate,
    Instance,
    Job,
    PhaseTiming,
    PlacementAttempt,
    RegionProbe,
)
from aivo.placement.estimates import Estimator
from aivo.placement.scorer import Context, LiveInstance
from aivo.providers.base import GpuOffer, traits_for
from aivo.worker_client.client import WorkerClient

FAILURE_OUTCOMES = ("capacity", "quota", "late_failure", "boot_failure", "error")


def utcnow() -> datetime:
    return datetime.now(UTC)


async def get_offers(app: AppContext, *, max_age_s: float = 60, force: bool = False) -> list[GpuOffer]:
    oc = app.offers
    if force or not oc.offers or time.monotonic() - oc.fetched_at > max_age_s:
        try:
            offers = await app.provider.list_offers()
            for i, o in enumerate(offers):
                if o.gpu_class is None:
                    gc = app.catalog.gpu_class_for(
                        o.gpu_model, o.size_slug.split("-")[1] if "-" in o.size_slug else None
                    )
                    if gc:
                        offers[i] = GpuOffer(**{**o.__dict__, "gpu_class": gc})
            oc.offers, oc.fetched_at, oc.error = offers, time.monotonic(), None
        except Exception as e:  # keep serving the last known list
            oc.error = str(e)
            if not oc.offers:
                raise
    return oc.offers


def effective_idle_timeout(app: AppContext, rs: RuntimeSettings) -> float:
    """The general idle timeout, capped by the provider (RunPod Serverless keeps idle workers briefly)."""
    cap = traits_for(app.provider.name).max_idle_s
    return float(rs.idle_timeout_s if cap is None else min(rs.idle_timeout_s, cap))


def effective_min_billing(app: AppContext, rs: RuntimeSettings) -> float:
    m = traits_for(app.provider.name).min_billing_s
    return float(rs.min_billing_s if m is None else m)


def offer_from_instance(inst: Instance) -> GpuOffer:
    return GpuOffer(
        provider=inst.provider,
        size_slug=inst.size_slug,
        region=inst.region,
        gpu_model=inst.gpu_model,
        gpu_class=inst.gpu_class,
        gpu_count=1,
        vram_gb=inst.vram_gb,
        vcpus=0,
        ram_gb=inst.ram_gb or 0,
        disk_gb=10_000,
        scratch_gb=0,
        price_hourly=float(inst.price_hourly),
        vendor=inst.vendor,  # type: ignore[arg-type]
    )


async def build_context(app: AppContext, session: AsyncSession, rs: RuntimeSettings, *, allow_new: bool) -> Context:
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
    cooldown: set[tuple[str, str]] = set()
    for size, region, outcome, at in attempts:
        if outcome in ("volume_busy", "unknown_adopted", "booted"):
            continue
        age_h = (now - at).total_seconds() / 3600
        w = 0.5 ** (age_h / 6)
        key = (size, region)
        totals[key] = totals.get(key, 0) + w
        if outcome in FAILURE_OUTCOMES:
            weight = 2.0 if outcome in ("late_failure", "boot_failure") else 1.0
            fails[key] = fails.get(key, 0) + w * weight
            if (now - at).total_seconds() < rs.capacity_cooldown_s and outcome in ("capacity", "quota"):
                cooldown.add(key)
    p_fail = {k: (fails.get(k, 0) + 1) / (totals[k] + 5) for k in totals}

    latency: dict[str, float] = {}
    for region, rtt in (
        await session.execute(
            select(RegionProbe.region, RegionProbe.rtt_ms).order_by(RegionProbe.measured_at.desc()).limit(200)
        )
    ).all():
        latency.setdefault(region, rtt)

    caches = (
        (await session.execute(select(CacheResource).where(CacheResource.status.in_(("ready", "verified")))))
        .scalars()
        .all()
    )
    snapshot_regions = {c.region for c in caches if c.kind == "snapshot" and c.region}
    volume_files = {
        c.region: frozenset(c.contents or [])
        for c in caches
        if c.kind == "volume" and c.region and c.attached_instance_id is None
    }
    est_values = {e.key: e.ewma for e in (await session.execute(select(Estimate))).scalars().all()}

    return Context(
        preference=rs.preference_default,
        allowed_regions=frozenset(rs.allowed_regions),
        preferred_regions=frozenset(rs.preferred_regions),
        denied_sizes=frozenset(rs.denied_sizes),
        max_price_hourly=rs.max_price_hourly,
        idle_timeout_s=effective_idle_timeout(app, rs),
        min_billing_s=effective_min_billing(app, rs),
        create_unbilled=traits_for(app.provider.name).bills_from_boot,
        tail_share=await tail_share(session, rs),
        allow_new=allow_new,
        estimator=Estimator(est_values),
        gpu_perf={g.id: g.perf for g in app.catalog.gpu_classes.values()},
        p_fail=p_fail,
        cooldown=frozenset(cooldown),
        latency_ms=latency,
        snapshot_regions=frozenset(snapshot_regions),
        volume_files=volume_files,
        mirror_available=bool(app.env.spaces_bucket) and rs.cache_policy.mirror_enabled,
    )


async def tail_share(session: AsyncSession, rs: RuntimeSettings) -> float:
    """1 - P(another job arrives before the idle timeout), from the last 30 days."""
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
    followups = sum(1 for g in gaps if g < rs.idle_timeout_s)
    return max(0.1, 1 - followups / len(gaps))


async def live_view(session: AsyncSession, app: AppContext) -> tuple[list[Instance], list[LiveInstance]]:
    insts = (
        (
            await session.execute(
                select(Instance).where(
                    Instance.state.in_(("pending", "creating", "provisioning", "booting", "ready", "busy")),
                    Instance.drain_reason.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    # remaining work reserved on each machine
    reserved = dict(
        (
            await session.execute(
                select(Job.instance_id, func.coalesce(func.sum(Job.est_wait_s), 0))
                .where(
                    Job.instance_id.is_not(None),
                    Job.state.in_(("provisioning", "queued", "assigned", "preparing", "running")),
                )
                .group_by(Job.instance_id)
            )
        ).all()
    )
    out = []
    for i in insts:
        boot_left = 0.0
        if i.state in ("pending", "creating", "provisioning", "booting"):
            boot_left = 180.0
        out.append(
            LiveInstance(
                instance_id=i.id,
                offer=offer_from_instance(i),
                state=i.state,
                files_present=frozenset(i.files_present or []),
                models_loaded=tuple(i.models_loaded or []),
                queue_s=float(reserved.get(i.id, 0) or 0) * 0.5,
                remaining_boot_s=boot_left,
            )
        )
    return list(insts), out


def worker_client(app: AppContext, inst: Instance) -> WorkerClient:
    if inst.endpoint_url and inst.token_enc:  # RunPod Serverless: through the gateway, pinned to its worker
        token = app.keyring.open(inst.token_enc, inst.id.bytes)
        api_key = app.env.runpod_api_key
        assert token is not None
        if api_key is None:
            raise RuntimeError("AIVO_RUNPOD_API_KEY is not set")
        return WorkerClient.via_gateway(
            inst.endpoint_url,
            token,
            api_key=api_key.get_secret_value(),
            worker_id=inst.worker_ref,
        )
    if not (inst.public_ipv4 and inst.cert_ca_pem and inst.sni_name and inst.token_enc):
        raise RuntimeError("instance has no connection details yet")
    token = app.keyring.open(inst.token_enc, inst.id.bytes)
    assert token is not None
    return WorkerClient(inst.public_ipv4, inst.port, inst.cert_ca_pem, inst.sni_name, token)


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
            size_slug=inst.size_slug if inst else None,
            gpu_class=inst.gpu_class if inst else None,
            region=inst.region if inst else None,
            image_kind=inst.image_kind if inst else None,
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
