from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.api.deps import current_user, get_app, get_session
from aivo.api.schemas import EstimateOut, JobCreateIn, JobOut, Page
from aivo.context import AppContext
from aivo.controller.lifecycle import transition_job
from aivo.db.models import Asset, Job, User
from aivo.services.jobs import create_job, estimate, job_out

router = APIRouter(tags=["jobs"])


@router.post("/estimate")
async def post_estimate(
    body: JobCreateIn,
    _: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> EstimateOut:
    key = body.placement.offer_key if body.placement.mode == "manual" else None
    return await estimate(app, s, body, body.preference, manual_offer_key=key)


@router.post("/jobs", status_code=201)
async def post_job(
    body: JobCreateIn,
    idempotency_key: str | None = Header(default=None, max_length=80),
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> JobOut:
    rs = await app.runtime(s)
    job = await create_job(app, s, user, body, idempotency_key, rs)
    await s.commit()
    if app.controller:
        app.controller.wake()
    return await job_out(app, s, job, user.display_name)


@router.get("/jobs")
async def list_jobs(
    active: bool = False,
    limit: int = Query(20, le=100),
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> list[JobOut]:
    q = select(Job, User.display_name).join(User, User.id == Job.user_id).where(Job.deleted_at.is_(None))
    if active:
        q = q.where(Job.state.notin_(("succeeded", "failed", "cancelled")))
    if user.role != "admin":
        q = q.where(Job.user_id == user.id)
    rows = (await s.execute(q.order_by(Job.created_at.desc()).limit(limit))).all()
    return [await job_out(app, s, j, name) for j, name in rows]


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> JobOut:
    row = (
        await s.execute(
            select(Job, User.display_name)
            .join(User, User.id == Job.user_id)
            .where(Job.id == job_id, Job.deleted_at.is_(None))
        )
    ).first()
    if row is None:
        raise HTTPException(404, "Not found")
    return await job_out(app, s, row[0], row[1])


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> JobOut:
    job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None or (job.user_id != user.id and user.role != "admin"):
        raise HTTPException(404, "Not found")
    if job.state in ("queued", "waiting_capacity", "provisioning"):
        await transition_job(s, job.id, job.state, "cancelled", stage="cancelled", instance_id=None)
    elif job.state in ("assigned", "preparing", "running"):
        await s.execute(update(Job).where(Job.id == job.id).values(cancel_requested=True))
    await s.commit()
    await s.refresh(job)
    return await job_out(app, s, job)


@router.post("/jobs/{job_id}/retry", status_code=201)
async def retry_job(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> JobOut:
    job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None or (job.user_id != user.id and user.role != "admin"):
        raise HTTPException(404, "Not found")
    if job.state != "failed":
        raise HTTPException(409, "Only failed creations can be retried")
    await transition_job(
        s,
        job.id,
        "failed",
        "queued",
        stage="queued",
        attempt=0,
        error_code=None,
        error_message=None,
        instance_id=None,
        capacity_wait_until=None,
        next_attempt_at=None,
        finished_at=None,
        progress=0.0,
    )
    await s.commit()
    if app.controller:
        app.controller.wake()
    await s.refresh(job)
    return await job_out(app, s, job)


@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None or (job.user_id != user.id and user.role != "admin"):
        raise HTTPException(404, "Not found")
    if job.state not in ("succeeded", "failed", "cancelled"):
        raise HTTPException(409, "Cancel it first")
    assets = (await s.execute(select(Asset).where(Asset.job_id == job.id))).scalars().all()
    for a in assets:
        app.store.delete(a.storage_path)
        a.deleted_at = datetime.now(UTC)
    job.deleted_at = datetime.now(UTC)
    # erase the sealed prompt too
    job.prompt_enc = job.avoid_enc = job.lyrics_enc = None
    await s.commit()
    return {"ok": True}


@router.get("/gallery")
async def gallery(
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = Query(30, le=100),
    mine: bool = False,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> Page:
    q = (
        select(Job, User.display_name)
        .join(User, User.id == Job.user_id)
        .where(Job.state == "succeeded", Job.deleted_at.is_(None))
    )
    if kind in ("image", "video", "music"):
        q = q.where(Job.kind == kind)
    if mine:
        q = q.where(Job.user_id == user.id)
    if cursor:
        q = q.where(Job.created_at < datetime.fromisoformat(cursor))
    rows = (await s.execute(q.order_by(Job.created_at.desc()).limit(limit + 1))).all()
    items = [await job_out(app, s, j, n) for j, n in rows[:limit]]
    nxt = rows[limit - 1][0].created_at.isoformat() if len(rows) > limit else None
    return Page(items=items, next_cursor=nxt)
