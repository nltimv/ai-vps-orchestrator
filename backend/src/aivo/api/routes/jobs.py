from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.api.deps import current_user, get_app, get_session, require_admin
from aivo.api.schemas import AdminJobOut, EstimateOut, JobCreateIn, JobOut
from aivo.context import AppContext
from aivo.controller.lifecycle import transition_job
from aivo.db.models import Asset, CollectionItem, Job, User
from aivo.safety import can_see, visible
from aivo.services.jobs import create_job, estimate, job_out

router = APIRouter(tags=["jobs"])

DONE = ("succeeded", "failed", "cancelled")


@router.post("/estimate")
async def post_estimate(
    body: JobCreateIn,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> EstimateOut:
    key = body.placement.offer_key if body.placement.mode == "manual" else None
    return await estimate(app, s, body, body.preference, manual_offer_key=key, user=user)


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
    """The user's own creations (admins too: see /admin/jobs for troubleshooting others')."""
    q = (
        select(Job, User.display_name)
        .join(User, User.id == Job.user_id)
        .where(Job.deleted_at.is_(None), Job.user_id == user.id)
    )
    q = visible(q, user, await app.runtime(s))
    if active:
        q = q.where(Job.state.notin_(DONE))
    rows = (await s.execute(q.order_by(Job.created_at.desc()).limit(limit))).all()
    return [await job_out(app, s, j, name) for j, name in rows]


@router.get("/admin/jobs")
async def admin_jobs(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(require_admin),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> list[AdminJobOut]:
    """Everyone's recent creations for troubleshooting: how they ran, never their words or results."""
    q = select(Job, User.display_name).join(User, User.id == Job.user_id).where(Job.deleted_at.is_(None))
    rows = (await s.execute(visible(q, user, await app.runtime(s)).order_by(Job.created_at.desc()).limit(limit))).all()
    counts = dict(
        (
            await s.execute(
                select(Asset.job_id, func.count())
                .where(Asset.job_id.in_([j.id for j, _ in rows]), Asset.role == "output", Asset.deleted_at.is_(None))
                .group_by(Asset.job_id)
            )
        ).all()
    )
    out = []
    for j, name in rows:
        model = app.catalog.models.get(j.model_id)
        out.append(
            AdminJobOut(
                id=j.id,
                user_name=name,
                kind=j.kind,
                model_name=model.manifest.display_name if model else j.model_id,
                state=j.state,
                stage=j.stage,
                gpu_type=j.gpu_type,
                error=j.error_message,
                results=counts.get(j.id, 0),
                cost_usd=j.attributed_cost_usd,
                created_at=j.created_at,
                finished_at=j.finished_at,
            )
        )
    return out


@router.post("/admin/jobs/{job_id}/cancel")
async def admin_cancel(
    job_id: uuid.UUID,
    _: User = Depends(require_admin),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Stop anyone's creation (e.g. one stuck on a GPU), without seeing it."""
    job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "Not found")
    await _cancel(app, s, job)
    return {"ok": True}


async def _cancel(app: AppContext, s: AsyncSession, job: Job) -> None:
    if job.state in ("queued", "waiting_capacity", "provisioning"):
        await transition_job(s, job.id, job.state, "cancelled", stage="cancelled", instance_id=None, reserved_usd=0)
    elif job.state in ("assigned", "preparing", "running", "fetching"):
        # the job runner cancels it at RunPod; a worker that has it stops at its next progress report
        await s.execute(update(Job).where(Job.id == job.id).values(cancel_requested=True))
    await s.commit()
    if app.controller:
        app.controller.wake()


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
    if row is None or row[0].user_id != user.id or not can_see(row[0], user, await app.runtime(s)):
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
    if job is None or job.user_id != user.id:
        raise HTTPException(404, "Not found")
    await _cancel(app, s, job)
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
    if job is None or job.user_id != user.id or not can_see(job, user, await app.runtime(s)):
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
        min_vram_gb=None,
        runpod_job_id=None,
        endpoint_runpod_id=None,
        endpoint_id=None,
        ticket_hash=None,
        cancel_requested=False,
        log=[],
    )
    await s.commit()
    if app.controller:
        app.controller.wake()
    await s.refresh(job)
    return await job_out(app, s, job)


@router.get("/jobs/{job_id}/log")
async def job_log(
    job_id: uuid.UUID,
    since: int = Query(0, ge=0),
    _: User = Depends(require_admin),
    s: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """The worker's log lines for this creation (admins, anyone's; for troubleshooting). Poll with ``since``.
    The lines say how it ran (downloads, GPU, errors), not what it shows: workers never log descriptions."""
    job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "Not found")
    lines = job.log or []
    return {
        "state": job.state,
        "stage": job.stage,
        "gpu_type": job.gpu_type,
        "total": len(lines),
        "lines": lines[since:] if since <= len(lines) else lines,
        "error": job.error_message,
    }


@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Delete a whole run: everything made together in it (one item at a time: DELETE /items/{id})."""
    job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None or job.user_id != user.id:
        raise HTTPException(404, "Not found")
    if job.state not in DONE:
        raise HTTPException(409, "Cancel it first")
    assets = (await s.execute(select(Asset).where(Asset.job_id == job.id))).scalars().all()
    for a in assets:
        app.store.delete(a.storage_path)
        a.deleted_at = datetime.now(UTC)
    await s.execute(delete(CollectionItem).where(CollectionItem.asset_id.in_([a.id for a in assets])))
    job.deleted_at = datetime.now(UTC)
    # erase the sealed words too
    job.prompt_enc = job.avoid_enc = job.lyrics_enc = job.prompt_used_enc = None
    await s.commit()
    return {"ok": True}
