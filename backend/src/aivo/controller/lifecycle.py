"""Instance and job state machines.

Every transition is a compare-and-set (``UPDATE ... WHERE id = ? AND state IN (...)``)
and only proceeds when exactly one row changed. That makes concurrent actors
(scheduler, reconciler, job runners, a second controller during a rollout) safe
without relying on leadership for correctness.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.db.models import Instance, Job
from aivo.events.bus import emit

LIVE_INSTANCE_STATES = (
    "pending", "creating", "provisioning", "booting", "ready", "busy", "draining", "destroying",
)
STARTING_STATES = ("pending", "creating", "provisioning", "booting")

INSTANCE_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"creating", "destroyed", "destroying"},
    "creating": {"pending", "provisioning", "destroying", "destroyed"},
    "provisioning": {"booting", "destroying"},
    "booting": {"ready", "destroying"},
    "ready": {"busy", "draining", "destroying"},
    "busy": {"ready", "draining", "destroying"},
    "draining": {"destroying", "ready"},
    "destroying": {"destroyed"},
    "destroyed": set(),
}

ACTIVE_JOB_STATES = ("assigned", "preparing", "running", "fetching")
WAITING_JOB_STATES = ("queued", "waiting_capacity", "provisioning")
TERMINAL_JOB_STATES = ("succeeded", "failed", "cancelled")

JOB_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"provisioning", "assigned", "waiting_capacity", "failed", "cancelled", "queued"},
    "waiting_capacity": {"queued", "provisioning", "assigned", "failed", "cancelled"},
    "provisioning": {"assigned", "queued", "waiting_capacity", "failed", "cancelled"},
    "assigned": {"preparing", "queued", "failed", "cancelled"},
    "preparing": {"running", "queued", "failed", "cancelled"},
    "running": {"fetching", "queued", "failed", "cancelled"},
    "fetching": {"succeeded", "queued", "failed"},
    "succeeded": set(),
    "failed": {"queued"},  # manual retry
    "cancelled": set(),
}


class IllegalTransition(Exception):
    pass


def _check(table: dict[str, set[str]], frm: tuple[str, ...], to: str) -> None:
    for f in frm:
        if to not in table.get(f, set()):
            raise IllegalTransition(f"{f} -> {to}")


async def transition_instance(
    session: AsyncSession,
    instance_id: uuid.UUID,
    frm: str | tuple[str, ...],
    to: str,
    *,
    event_payload: dict[str, Any] | None = None,
    **fields: Any,
) -> bool:
    frm_t = (frm,) if isinstance(frm, str) else tuple(frm)
    _check(INSTANCE_TRANSITIONS, frm_t, to)
    res = await session.execute(
        update(Instance)
        .where(Instance.id == instance_id, Instance.state.in_(frm_t))
        .values(state=to, state_version=Instance.state_version + 1, state_changed_at=func.now(), **fields)
        .returning(Instance.id)
    )
    if res.first() is None:
        return False
    await emit(
        session, scope="instance", type="instance.state", ref_id=instance_id,
        payload={"state": to, **(event_payload or {})},
    )
    return True


async def transition_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    frm: str | tuple[str, ...],
    to: str,
    *,
    user_id: uuid.UUID | None = None,
    event_payload: dict[str, Any] | None = None,
    **fields: Any,
) -> bool:
    frm_t = (frm,) if isinstance(frm, str) else tuple(frm)
    _check(JOB_TRANSITIONS, frm_t, to)
    if to in TERMINAL_JOB_STATES and "finished_at" not in fields:
        fields["finished_at"] = func.now()
    res = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.state.in_(frm_t))
        .values(state=to, **fields)
        .returning(Job.id, Job.user_id)
    )
    row = res.first()
    if row is None:
        return False
    payload = {"state": to}
    for k in ("stage", "progress", "error_message", "error_code", "eta_s"):
        if k in fields and not hasattr(fields[k], "__clause_element__"):
            payload[k] = fields[k]
    payload.update(event_payload or {})
    await emit(session, scope="job", type="job.state", ref_id=job_id, user_id=user_id or row.user_id, payload=payload)
    return True


async def job_progress(
    session: AsyncSession, job: Job, *, progress: float, stage: str, eta_s: float | None = None
) -> None:
    """Non-state update (progress/stage) with an event, used by job runners."""
    await session.execute(
        update(Job).where(Job.id == job.id).values(progress=progress, stage=stage, eta_s=eta_s)
    )
    await emit(
        session, scope="job", type="job.progress", ref_id=job.id, user_id=job.user_id,
        payload={"progress": round(progress, 3), "stage": stage, "eta_s": eta_s},
    )
