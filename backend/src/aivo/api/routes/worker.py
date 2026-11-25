"""The API RunPod workers call (``/api/v1/worker/jobs/{id}/...``), authorized by the job's one-time ticket.

A ticket is only valid while its job is with a worker (or waiting for one); it stops working when the job ends,
is cancelled or moves to another GPU type. aivo stores only its SHA-256. Results arrive here as plaintext over
HTTPS, are encrypted onto the media volume right away, and never touch RunPod's job system.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.api.deps import get_app, get_session
from aivo.catalog.presets import describe
from aivo.context import AppContext
from aivo.controller.common import record_phase, update_estimate, utcnow
from aivo.controller.jobspec import (
    FRIENDLY_ERRORS,
    accept_rewrite,
    download_error,
    friendly_from_job,
    job_files,
    work_spec,
)
from aivo.controller.lifecycle import job_progress, transition_job
from aivo.controller.workers import job_cost, worker_done, worker_session
from aivo.db.models import Asset, GpuEndpoint, Instance, Job
from aivo.storage.thumbnails import make_thumbnail
from aivo_protocol import PROTOCOL_VERSION, protocol_compatible, protocol_tuple
from aivo_protocol.worker_api import (
    CompleteIn,
    FailIn,
    LogLine,
    ProgressIn,
    ProgressOut,
    PromptIn,
    PromptOut,
    UploadOut,
    WorkerInfo,
    WorkSpec,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/worker", tags=["worker"])

ACTIVE = ("assigned", "preparing", "running", "fetching")
LOG_KEEP = 300
MAX_UPLOAD = 4 * 1024**3
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MEDIA_TYPE = re.compile(r"^(image|video|audio)/[a-z0-9.+-]{1,40}$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def ticket_job(
    job_id: uuid.UUID,
    authorization: str = Header(default=""),
    s: AsyncSession = Depends(get_session),
) -> Job:
    """The job this ticket is for, locked for update. The same answer for unknown jobs and wrong tickets."""
    scheme, _, token = authorization.partition(" ")
    job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
    if (
        scheme.lower() != "bearer"
        or not token
        or job is None
        or not job.ticket_hash
        or not hmac.compare_digest(job.ticket_hash, _hash(token))
    ):
        raise HTTPException(401, "invalid ticket")
    if job.state not in ACTIVE:
        raise HTTPException(410, "this job has ended")
    return job


def _append_log(job: Job, lines: list[LogLine]) -> None:
    if not lines:
        return
    new = [{**ln.model_dump(), "text": ln.text[:2000]} for ln in lines[-LOG_KEEP:]]
    job.log = [*(job.log or []), *new][-LOG_KEEP:]


async def _wake(app: AppContext) -> None:
    if app.controller is not None:
        app.controller.wake()


async def discard_outputs(app: AppContext, job_id: uuid.UUID) -> None:
    """Remove results a worker uploaded for a job that didn't succeed (or that a retry replaces)."""
    async with app.sessionmaker() as s, s.begin():
        assets = (
            (await s.execute(select(Asset).where(Asset.job_id == job_id, Asset.role.in_(("output", "thumb")))))
            .scalars()
            .all()
        )
        for a in assets:
            await asyncio.to_thread(app.store.delete, a.storage_path)
        await s.execute(delete(Asset).where(Asset.job_id == job_id, Asset.role.in_(("output", "thumb"))))


@router.post("/jobs/{job_id}/spec")
async def spec(
    body: WorkerInfo,
    job: Job = Depends(ticket_job),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> WorkSpec:
    if job.cancel_requested:
        raise HTTPException(410, "cancelled")
    if not protocol_compatible(body.protocol):
        raise HTTPException(409, f"this worker speaks protocol {body.protocol}; aivo needs {PROTOCOL_VERSION}")
    now = utcnow()
    endpoint = None
    if job.endpoint_id:
        endpoint = (await s.execute(select(GpuEndpoint).where(GpuEndpoint.id == job.endpoint_id))).scalar_one_or_none()
    inst = await worker_session(s, job, body, endpoint, app.env.runpod_region)
    retry = job.state in ("running", "fetching") or job.worker_contact_at is not None
    if job.worker_contact_at is None and job.submitted_at is not None:
        # from submitting until a worker had the job: the queue wait plus the worker's cold start
        await record_phase(
            s,
            phase="queue",
            seconds=(now - job.submitted_at).total_seconds(),
            inst=inst,
            job=job,
            estimate_key=f"create:snapshot:{job.gpu_type}:{app.env.runpod_region}",
        )
    await transition_job(
        s,
        job.id,
        job.state,
        "preparing",
        stage="loading_model",
        progress=0.0,
        instance_id=inst.id,
        worker_contact_at=now,
        event_payload={"machine": f"{inst.gpu_model} in {app.env.runpod_region}"},
    )
    await s.commit()
    if retry:  # RunPod gave the job to a new worker: results of the old one don't count
        await discard_outputs(app, job.id)
    # workers before protocol 2.1 can't improve descriptions: they get the description as written
    version = protocol_tuple(body.protocol)
    return await asyncio.to_thread(work_spec, app, job, can_rewrite=version >= (2, 1), can_batch=version >= (2, 2))


@router.post("/jobs/{job_id}/prompt")
async def prompt(
    body: PromptIn,
    job: Job = Depends(ticket_job),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> PromptOut:
    """The worker's improved description: checked, kept (sealed) with the creation, and the words to use.

    If it can't be used, the creation is made from the description as the user wrote it.
    """
    rw = app.catalog.rewriter
    fp = friendly_from_job(app, job)
    mf = app.catalog.get(job.model_id).manifest
    fallback = describe(app.catalog, mf, fp)
    text, why = (
        (None, "rewriting is off")
        if rw is None
        else accept_rewrite(rw, body.text, adult=bool(job.adult), original=fp.prompt)
    )
    if body.error and text is None:
        why = f"the language model failed: {body.error[:200]}"
    took = f" in {body.seconds:.1f} s" if body.seconds is not None else ""
    note = f"description improved{took}" if text else f"kept the description as written: {why}"
    _append_log(job, [LogLine(seq=0, ts=utcnow().timestamp(), source="aivo", text=note)])
    job.prompt_used_enc = app.keyring.seal(text, job.id.bytes) if text else None
    await s.commit()
    return PromptOut(prompt=text or fallback, rewritten=text is not None)


@router.get("/jobs/{job_id}/inputs/{name}")
async def get_input(name: str, job: Job = Depends(ticket_job), app: AppContext = Depends(get_app)) -> Response:
    ref = next((i for i in (job.resolved or {}).get("inputs", []) if i.get("name") == name), None)
    if ref is None:
        raise HTTPException(404, "no such input")
    data = await asyncio.to_thread(app.store.read_all, uuid.UUID(ref["asset_id"]), ref["storage_path"])
    return Response(content=data, media_type="application/octet-stream")


@router.post("/jobs/{job_id}/progress")
async def progress(
    body: ProgressIn,
    job: Job = Depends(ticket_job),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> ProgressOut:
    _append_log(job, body.lines)
    now = utcnow()
    job.worker_contact_at = now
    if job.instance_id:
        await s.execute(update(Instance).where(Instance.id == job.instance_id).values(last_contact_at=now))
    stage = body.stage if body.stage in ("loading_model", "improving", "generating", "saving") else "generating"
    if stage == "generating" and job.state in ("assigned", "preparing"):
        await transition_job(
            s, job.id, job.state, "running", stage="generating", progress=body.progress, started_at=now
        )
    else:
        await job_progress(s, job, progress=max(0.0, min(1.0, body.progress)), stage=stage, eta_s=body.eta_s)
    cancel = bool(job.cancel_requested)
    await s.commit()
    return ProgressOut(cancel=cancel)


@router.put("/jobs/{job_id}/outputs/{name}")
async def upload_output(
    name: str,
    request: Request,
    media_type: str = Query(...),
    sha256: str = Query(..., pattern=r"^[0-9a-f]{64}$"),
    job: Job = Depends(ticket_job),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> UploadOut:
    if not _SAFE_NAME.match(name) or not _MEDIA_TYPE.match(media_type):
        raise HTTPException(400, "bad name or media type")
    job_id, owner = job.id, job.user_id
    await s.commit()  # don't hold the row lock while the file streams in
    scratch = app.env.scratch_dir / f"up-{job_id.hex}-{uuid.uuid4().hex[:8]}"
    h = hashlib.sha256()
    size = 0
    try:
        with scratch.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "too large")
                h.update(chunk)
                fh.write(chunk)
        digest = h.hexdigest()
        if digest != sha256:
            raise HTTPException(422, "checksum mismatch")
        aid = uuid.uuid4()
        stored = await asyncio.to_thread(app.store.put_file, aid, scratch)
        thumb = await make_thumbnail(scratch, media_type)
    finally:
        scratch.unlink(missing_ok=True)
    tstored = None
    tid = uuid.uuid4()
    if thumb:
        tstored = await asyncio.to_thread(app.store.put_bytes, tid, thumb)
    async with app.sessionmaker() as s2, s2.begin():
        row = (await s2.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
        if row.state not in ACTIVE:
            await asyncio.to_thread(app.store.delete, stored.storage_path)
            if tstored:
                await asyncio.to_thread(app.store.delete, tstored.storage_path)
            raise HTTPException(410, "this job has ended")
        old = (await s2.execute(select(Asset).where(Asset.job_id == job_id, Asset.output_name == name))).scalars().all()
        for a in old:  # a retried upload replaces the earlier copy
            await asyncio.to_thread(app.store.delete, a.storage_path)
            await s2.delete(a)
        s2.add(
            Asset(
                id=aid,
                owner_user_id=owner,
                job_id=job_id,
                output_name=name,
                role="output",
                media_type=media_type,
                storage_path=stored.storage_path,
                plaintext_size=stored.plaintext_size,
                ciphertext_size=stored.ciphertext_size,
                key_id=stored.key_id,
                content_mac=stored.content_mac,
            )
        )
        if tstored:
            s2.add(
                Asset(
                    id=tid,
                    owner_user_id=owner,
                    job_id=job_id,
                    parent_asset_id=aid,
                    output_name=name,
                    role="thumb",
                    media_type="image/webp",
                    storage_path=tstored.storage_path,
                    plaintext_size=tstored.plaintext_size,
                    ciphertext_size=tstored.ciphertext_size,
                    key_id=tstored.key_id,
                    content_mac=tstored.content_mac,
                )
            )
        if row.state in ("assigned", "preparing", "running"):
            await transition_job(s2, job_id, row.state, "fetching", stage="saving", progress=1.0)
    return UploadOut(bytes=size, sha256=digest)


@router.post("/jobs/{job_id}/complete")
async def complete(
    body: CompleteIn,
    job: Job = Depends(ticket_job),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    _append_log(job, body.lines)
    assets = (await s.execute(select(Asset).where(Asset.job_id == job.id, Asset.role == "output"))).scalars().all()
    by_name = {a.output_name: a for a in assets}
    missing = [o.name for o in body.outputs if o.name not in by_name]
    if missing or not body.outputs:
        raise HTTPException(409, f"results not delivered: {missing or 'none'}")
    for o in body.outputs:
        a = by_name[o.name]
        a.width, a.height, a.duration_s = o.width, o.height, o.duration_s
    inst = None
    if job.instance_id:
        inst = (await s.execute(select(Instance).where(Instance.id == job.instance_id))).scalar_one_or_none()
    price = float(inst.price_hourly) if inst else 0.0
    if job.state != "fetching":
        await transition_job(s, job.id, job.state, "fetching", stage="saving", progress=1.0)
    await transition_job(
        s,
        job.id,
        "fetching",
        "succeeded",
        stage="done",
        progress=1.0,
        eta_s=0,
        attributed_cost_usd=job_cost(price, body.worker, body.finished_at),
        ticket_hash=None,
        reserved_usd=0,
        event_payload={"outputs": [str(by_name[o.name].id) for o in body.outputs]},
    )
    await worker_done(s, job.instance_id, datetime.fromtimestamp(body.finished_at, UTC))
    resolved = job.resolved or {}
    for ph in body.phases:
        if ph.phase == "inference" and resolved.get("units") and inst is not None:
            await record_phase(
                s,
                phase="inference",
                seconds=ph.seconds,
                inst=inst,
                job=job,
                units=resolved["units"],
                quality=resolved.get("quality"),
            )
            await update_estimate(
                s,
                f"run:{job.model_id}:{resolved.get('quality')}:{inst.gpu_class}",
                ph.seconds / max(resolved["units"], 0.001),
            )
        elif ph.phase == "file_fetch" and ph.source and ph.bytes and ph.seconds > 0.5 and inst is not None:
            await record_phase(
                s, phase="file_fetch", seconds=ph.seconds, inst=inst, job=job, source=ph.source, bytes_=ph.bytes
            )
            await update_estimate(s, f"thr:{ph.source}:{inst.region}", ph.bytes / 1e9 / ph.seconds)
    if inst is not None:
        inst.models_loaded = [job.model_id]
    await s.commit()
    await _wake(app)
    return {"ok": True}


@router.post("/jobs/{job_id}/fail")
async def fail(
    body: FailIn,
    job: Job = Depends(ticket_job),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    _append_log(job, body.lines)
    finished = datetime.fromtimestamp(body.finished_at, UTC) if body.finished_at else utcnow()
    log.warning("job %s failed on the worker (%s): %s", job.id, body.code, body.message[:300])
    common: dict[str, Any] = {"ticket_hash": None, "reserved_usd": 0}
    if body.code == "cancelled" and job.cancel_requested:
        await transition_job(s, job.id, job.state, "cancelled", stage="cancelled", **common)
    elif body.code == "oom" and job.attempt + 1 < job.max_attempts:
        # try again on a GPU with more memory
        vram = None
        if job.instance_id:
            vram = (
                await s.execute(select(Instance.vram_gb).where(Instance.id == job.instance_id))
            ).scalar_one_or_none()
        await transition_job(
            s,
            job.id,
            job.state,
            "queued",
            stage="queued",
            progress=0.0,
            attempt=job.attempt + 1,
            min_vram_gb=(vram or 0) + 1,
            instance_id=None,
            endpoint_id=None,
            runpod_job_id=None,
            endpoint_runpod_id=None,
            **common,
        )
    else:
        code = body.code if body.code in FRIENDLY_ERRORS else "engine"
        msg = download_error(body.message, job_files(app, job)) if code == "missing_files" else FRIENDLY_ERRORS[code]
        await transition_job(
            s, job.id, job.state, "failed", stage="failed", error_code=code, error_message=msg, **common
        )
    await worker_done(s, job.instance_id, finished)
    await s.commit()
    await discard_outputs(app, job.id)
    await _wake(app)
    return {"ok": True}
