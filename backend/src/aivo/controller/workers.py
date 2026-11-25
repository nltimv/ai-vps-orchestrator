"""RunPod workers as billing records (``instances`` rows).

RunPod bills a worker from its start (cold start included) until it scales down, which happens ``keep_warm_s``
after its last job. Workers report who they are with every job (``WorkerInfo``), so aivo can count:

* a worker session: from ``started_at`` until the last job ended + the keep-warm time (or now while it lives);
* per job: the time since the worker's previous job ended (or since it started) until this job ended.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.config import RuntimeSettings
from aivo.controller.common import utcnow
from aivo.db.models import GpuEndpoint, Instance, Job
from aivo.events.bus import emit
from aivo.providers.regions import region_meta
from aivo_protocol.worker_api import WorkerInfo


def ts(unix: float | None) -> datetime | None:
    return datetime.fromtimestamp(unix, UTC) if unix else None


async def worker_session(
    s: AsyncSession, job: Job, info: WorkerInfo, endpoint: GpuEndpoint | None, region: str
) -> Instance:
    """The live billing record of this worker, started if this is a new worker (or a worker id RunPod reused)."""
    now = utcnow()
    started = ts(info.started_at) or now
    row = (
        await s.execute(
            select(Instance)
            .where(
                Instance.provider == "runpod",
                Instance.provider_instance_id == info.worker_id,
                Instance.state != "destroyed",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is not None and row.create_accepted_at and abs((row.create_accepted_at - started).total_seconds()) > 5:
        await _end(s, row, row.idle_since or row.last_contact_at or now)  # the old run of this worker id
        row = None
    if row is None:
        gpu_name = (info.gpu.name if info.gpu else None) or (endpoint.gpu_name if endpoint else "GPU")
        row = Instance(
            id=uuid.uuid4(),
            name=f"{info.worker_id[:50]}-{int(info.started_at)}"[:80],
            provider="runpod",
            provider_instance_id=info.worker_id[:64],
            offer_key=f"runpod:{job.gpu_type}:{region}"[:128],
            size_slug=(job.gpu_type or "unknown")[:64],
            region=region[:32],
            gpu_class=(endpoint.gpu_class if endpoint else None) or "unknown",
            gpu_model=(endpoint.gpu_name if endpoint else gpu_name)[:64],
            vendor="nvidia",
            vram_gb=float(endpoint.vram_gb) if endpoint else (info.gpu.vram_total_gb if info.gpu else 0),
            ram_gb=0,
            price_hourly=endpoint.price_hourly if endpoint else 0,
            state="busy",
            create_accepted_at=started,
            ready_at=now,
            last_contact_at=now,
            endpoint_id=endpoint.id if endpoint else None,
            worker_image=info.agent_version[:256],
        )
        s.add(row)
        await s.flush()
        await emit(s, scope="instance", type="instance.state", ref_id=row.id, payload={"state": "busy"})
    else:
        row.state, row.last_contact_at, row.idle_since = "busy", now, None
    return row


async def worker_done(s: AsyncSession, inst_id: uuid.UUID | None, finished_at: datetime | None) -> None:
    """The worker finished a job and now waits for the next one (still billed)."""
    if inst_id is None:
        return
    now = utcnow()
    await s.execute(
        update(Instance)
        .where(Instance.id == inst_id, Instance.state != "destroyed")
        .values(state="ready", idle_since=finished_at or now, last_contact_at=now)
    )
    await emit(s, scope="instance", type="instance.state", ref_id=inst_id, payload={"state": "ready"})


def job_cost(price_hourly: float, info: WorkerInfo | None, finished_at: float | None) -> float | None:
    """This job's share: the worker's time since its previous job (or its start) until this job ended."""
    if info is None or finished_at is None:
        return None
    since = max(info.started_at, info.prev_job_end or 0)
    return round(price_hourly * max(0.0, finished_at - since) / 3600, 4)


async def _end(s: AsyncSession, row: Instance, last_activity: datetime) -> None:
    row.state = "destroyed"
    row.outcome = row.outcome or "ok"
    row.destroy_confirmed_at = max(row.create_accepted_at or last_activity, last_activity)
    await emit(s, scope="instance", type="instance.state", ref_id=row.id, payload={"state": "destroyed"})


async def close_finished_workers(s: AsyncSession, rs: RuntimeSettings) -> int:
    """Workers RunPod has scaled down by now: waiting longer than keep-warm, or silent far beyond a job's time."""
    now = utcnow()
    keep = timedelta(seconds=rs.keep_warm_s)
    rows = (
        (
            await s.execute(
                select(Instance)
                .where(Instance.provider == "runpod", Instance.state.in_(("ready", "busy")))
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    n = 0
    for row in rows:
        if row.state == "ready" and row.idle_since and now - row.idle_since > keep:
            await _end(s, row, row.idle_since + keep)
            n += 1
        elif row.state == "busy" and row.last_contact_at:
            silent = now - row.last_contact_at
            if silent > timedelta(seconds=rs.job_timeout_s + 1800):
                await _end(s, row, row.last_contact_at + keep)
                n += 1
    return n


def region_name(region: str) -> str:
    return region_meta(region).name
