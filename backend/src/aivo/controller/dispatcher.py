"""Run one assigned job on its machine, end to end.

preparing -> make sure model files are on the machine, upload the starting image
running   -> submit the rendered workflow, long-poll progress
fetching  -> download outputs into RAM scratch, verify, encrypt onto the media
             volume, make (encrypted) thumbnails, then tell the worker to delete them

Generated content only ever persists on the backend's encrypted media volume.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid

import httpx
from sqlalchemy import select, update

from aivo.cache.spaces import SpacesMirror, hf_url
from aivo.catalog.presets import render_workflow
from aivo.context import AppContext
from aivo.controller.common import record_phase, update_estimate, utcnow, worker_client
from aivo.controller.lifecycle import job_progress, transition_instance, transition_job
from aivo.db.models import Asset, Instance, Job
from aivo.events.bus import emit
from aivo.storage.thumbnails import make_thumbnail
from aivo.worker_client.client import WorkerClient, WorkerUnreachable
from aivo_protocol.worker_api import EnsureFilesIn, FileFetch, FileSource, JobIn, JobState, OutputSpec

log = logging.getLogger(__name__)

FRIENDLY_ERRORS = {
    "oom": "The machine ran out of memory for this request.",
    "timeout": "Creating this took too long and was stopped.",
    "missing_files": "The AI model could not be downloaded to the machine.",
    "engine": "Something went wrong while creating this.",
    "lost": "The machine stopped responding.",
}


class JobRunner:
    def __init__(self, app: AppContext, *, poll_s: float = 2.0, long_poll_s: float = 20.0):
        self.app = app
        self.poll_s = poll_s
        self.long_poll_s = long_poll_s
        self._mirror: SpacesMirror | None = None
        if app.env.spaces_bucket:
            try:
                self._mirror = SpacesMirror(app.env)
            except ValueError:
                self._mirror = None

    async def run(self, job_id: uuid.UUID) -> None:
        try:
            await self._run(job_id)
        except asyncio.CancelledError:
            raise
        except WorkerUnreachable as e:
            log.warning("job %s: worker unreachable: %s", job_id, e)
            await self._requeue_or_fail(job_id, "lost", lost_instance=True)
        except Exception as e:
            log.exception("job %s crashed", job_id)
            await self._finish_failed(job_id, "engine", f"{type(e).__name__}: {e}")

    async def _load(self, job_id: uuid.UUID) -> tuple[Job, Instance] | None:
        async with self.app.sessionmaker() as s:
            job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
            if job is None or job.instance_id is None:
                return None
            inst = (await s.execute(select(Instance).where(Instance.id == job.instance_id))).scalar_one_or_none()
            if inst is None:
                return None
            return job, inst

    async def _run(self, job_id: uuid.UUID) -> None:
        loaded = await self._load(job_id)
        if loaded is None:
            return
        job, inst = loaded
        if job.cancel_requested:
            await self._cancel(job, inst, None)
            return
        cm = self.app.catalog.get(job.model_id)
        resolved = job.resolved
        async with worker_client(self.app, inst) as client:
            # resume after a restart: if the worker already has this job, skip ahead
            existing = await client.job(str(job.id))
            if job.state in ("assigned", "preparing") and existing is None:
                if not await self._prepare(job, inst, client, cm, resolved):
                    return
                await self._submit(job, inst, client, cm, resolved)
            elif existing is None and job.state in ("running", "fetching"):
                # the worker lost it (e.g. restarted); start over on this machine
                async with self.app.sessionmaker() as s, s.begin():
                    await transition_job(s, job.id, job.state, "queued", instance_id=None, stage="queued")
                await self._release_instance(inst.id)
                return
            elif job.state == "assigned":
                async with self.app.sessionmaker() as s, s.begin():
                    await transition_job(s, job.id, "assigned", "running", stage="generating", started_at=utcnow())
            status = await self._poll(job, client)
            if status is None:
                return
            if status.state == JobState.succeeded:
                await self._fetch(job, inst, client, status, resolved)
            elif status.state == JobState.cancelled:
                await self._cancel(job, inst, client)
            else:
                code = status.error_code or "engine"
                await client.delete_job(str(job.id))
                if code == "oom":
                    await self._requeue_or_fail(job.id, "oom", bump_vram=inst.vram_gb)
                else:
                    await self._finish_failed(job.id, code, status.error or "")
                await self._release_instance(inst.id)

    # ------------------------------------------------------------------ stages

    async def _prepare(self, job, inst, client: WorkerClient, cm, resolved) -> bool:  # type: ignore[no-untyped-def]
        t0 = time.monotonic()
        async with self.app.sessionmaker() as s, s.begin():
            await transition_job(s, job.id, "assigned", "preparing", stage="loading_model", progress=0.0)
        mf = cm.manifest
        variant = mf.variant(resolved["variant_id"])
        files = mf.files_for(variant, resolved["mode"])
        fetches = [self._fetch_spec(f, inst) for f in files]
        await client.ensure_files(EnsureFilesIn(files=fetches))
        dests = [f.dest for f in fetches]
        total = sum(f.bytes for f in files) or 1
        last_emit = 0.0
        while True:
            st = await client.files_status(dests)
            if st.all_ready:
                break
            if st.failed:
                await client.delete_job(str(job.id))
                await self._finish_failed(job.id, "missing_files", "; ".join(f.error or "" for f in st.failed)[:300])
                await self._release_instance(inst.id)
                return False
            done = sum(f.bytes_done for f in st.files)
            frac = min(0.99, done / total)
            if time.monotonic() - last_emit > 2:
                async with self.app.sessionmaker() as s, s.begin():
                    await job_progress(s, job, progress=frac, stage="loading_model")
                last_emit = time.monotonic()
            if await self._cancelled(job.id):
                await self._cancel(job, inst, client)
                return False
            await asyncio.sleep(self.poll_s)
        fetch_s = time.monotonic() - t0
        # starting image (decrypted only in memory, sent over the pinned channel)
        for ref in resolved.get("inputs", []):
            data = await asyncio.to_thread(self.app.store.read_all, uuid.UUID(ref["asset_id"]), ref["storage_path"])
            await client.put_input(ref["name"], data)
        async with self.app.sessionmaker() as s, s.begin():
            await s.execute(update(Instance).where(Instance.id == inst.id).values(files_present=sorted(set(inst.files_present or []) | set(dests))))
            if fetch_s > 1:
                await record_phase(s, phase="files_ready", seconds=fetch_s, inst=inst, job=job)
        return True

    def _fetch_spec(self, f, inst: Instance) -> FileFetch:  # type: ignore[no-untyped-def]
        sources: list[FileSource] = []
        if inst.volume_resource_id is not None:
            sources.append(FileSource(kind="volume", path=f.dest))
        if self._mirror is not None:
            try:
                sources.append(FileSource(kind="url", url=self._mirror.presign_get(f)))
            except Exception as e:  # mirror misconfigured: fall back to HF
                log.warning("presign failed: %s", e)
        sources.append(FileSource(kind="hf", url=self._hf_url(f)))
        return FileFetch(dest=f.dest, bytes=f.bytes if f.sha256 else None, sha256=f.sha256, sources=sources)

    def _hf_url(self, f) -> str:  # type: ignore[no-untyped-def]
        url = hf_url(f)
        if f.gated and self.app.env.hf_token:
            # resolve to a short-lived signed CDN URL so the worker never holds the HF token
            try:
                r = httpx.get(
                    url, headers={"Authorization": f"Bearer {self.app.env.hf_token.get_secret_value()}",
                                  "Range": "bytes=0-0"},
                    follow_redirects=False, timeout=20,
                )
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    return r.headers["location"]
            except httpx.HTTPError as e:
                log.warning("could not resolve gated download: %s", e)
        return url

    async def _submit(self, job, inst, client: WorkerClient, cm, resolved) -> None:  # type: ignore[no-untyped-def]
        workflow = render_workflow(cm.workflows[resolved["mode"]], resolved["params"])
        out = OutputSpec.model_validate(resolved["output"])
        mf = cm.manifest
        variant = mf.variant(resolved["variant_id"])
        await client.submit(JobIn(
            job_id=str(job.id), model_id=job.model_id, workflow=workflow,
            inputs=[r["name"] for r in resolved.get("inputs", [])],
            required_files=[f.dest for f in mf.files_for(variant, resolved["mode"])],
            output=out, timeout_s=int(resolved.get("timeout_s", 3600)),
        ))
        async with self.app.sessionmaker() as s, s.begin():
            await transition_job(s, job.id, "preparing", "running", stage="generating", progress=0.0,
                                 started_at=utcnow())

    async def _poll(self, job: Job, client: WorkerClient):  # type: ignore[no-untyped-def]
        last = (-1.0, "")
        last_t = 0.0
        while True:
            st = await client.job(str(job.id), wait=self.long_poll_s)
            if st is None:
                raise WorkerUnreachable("worker forgot the job")
            if st.state.terminal:
                return st
            if await self._cancelled(job.id):
                await client.cancel(str(job.id))
            stage = {"loading_model": "loading_model", "saving": "saving"}.get(st.stage, "generating")
            if (abs(st.progress - last[0]) >= 0.02 or stage != last[1]) and time.monotonic() - last_t > 0.5:
                async with self.app.sessionmaker() as s, s.begin():
                    await job_progress(s, job, progress=st.progress, stage=stage, eta_s=st.eta_s)
                    await s.execute(update(Instance).where(Instance.id == job.instance_id).values(last_contact_at=utcnow()))
                last, last_t = (st.progress, stage), time.monotonic()

    async def _fetch(self, job, inst, client: WorkerClient, status, resolved) -> None:  # type: ignore[no-untyped-def]
        t0 = time.monotonic()
        async with self.app.sessionmaker() as s, s.begin():
            await transition_job(s, job.id, "running", "fetching", stage="saving", progress=1.0)
        scratch = self.app.env.scratch_dir / f"job-{job.id}"
        scratch.mkdir(parents=True, exist_ok=True)
        assets: list[Asset] = []
        try:
            for o in status.outputs:
                tmp = scratch / o.name
                await client.download(str(job.id), o.name, tmp)
                digest = await asyncio.to_thread(lambda p=tmp: hashlib.sha256(p.read_bytes()).hexdigest())
                if digest != o.sha256:
                    raise WorkerUnreachable(f"checksum mismatch for {o.name}")
                aid = uuid.uuid4()
                stored = await asyncio.to_thread(self.app.store.put_file, aid, tmp)
                assets.append(Asset(
                    id=aid, owner_user_id=job.user_id, job_id=job.id, output_name=o.name, role="output",
                    media_type=o.media_type, storage_path=stored.storage_path, plaintext_size=stored.plaintext_size,
                    ciphertext_size=stored.ciphertext_size, key_id=stored.key_id, content_mac=stored.content_mac,
                    width=o.width, height=o.height, duration_s=o.duration_s,
                ))
                thumb = await make_thumbnail(tmp, o.media_type)
                if thumb:
                    tid = uuid.uuid4()
                    ts = await asyncio.to_thread(self.app.store.put_bytes, tid, thumb)
                    assets.append(Asset(
                        id=tid, owner_user_id=job.user_id, job_id=job.id, parent_asset_id=aid,
                        output_name=o.name, role="thumb", media_type="image/webp", storage_path=ts.storage_path,
                        plaintext_size=ts.plaintext_size, ciphertext_size=ts.ciphertext_size, key_id=ts.key_id,
                        content_mac=ts.content_mac,
                    ))
                tmp.unlink(missing_ok=True)
        finally:
            for p in scratch.glob("*"):
                p.unlink(missing_ok=True)
            scratch.rmdir()
        # outputs are safely encrypted on our volume -> worker may delete its copy
        await client.delete_job(str(job.id))
        try:
            tel = await client.telemetry()
        except Exception:
            tel = None
        run_s = (utcnow() - job.started_at).total_seconds() if job.started_at else None
        async with self.app.sessionmaker() as s, s.begin():
            for a in assets:
                s.add(a)
            await s.flush()
            price = float(inst.price_hourly)
            cost = round(price * (run_s or 0) / 3600, 4) if run_s else None
            await transition_job(
                s, job.id, "fetching", "succeeded", stage="done", progress=1.0, attributed_cost_usd=cost,
                event_payload={"outputs": [str(a.id) for a in assets if a.role == "output"]},
            )
            await record_phase(s, phase="transfer", seconds=time.monotonic() - t0, inst=inst, job=job)
            if tel:
                for ph in tel.phases:
                    if ph.phase == "inference" and resolved.get("units"):
                        await record_phase(
                            s, phase="inference", seconds=ph.seconds, inst=inst, job=job, units=resolved["units"],
                            quality=resolved.get("quality"),
                        )
                        # per-unit runtime estimate feeds the scorer
                        await update_estimate(
                            s, f"run:{job.model_id}:{resolved.get('quality')}:{inst.gpu_class}",
                            ph.seconds / max(resolved["units"], 0.001),
                        )
                    elif ph.phase == "file_fetch" and ph.source and ph.bytes and ph.seconds > 0.5:
                        await record_phase(s, phase="file_fetch", seconds=ph.seconds, inst=inst, job=job,
                                           source=ph.source, bytes_=ph.bytes)
                        await update_estimate(s, f"thr:{ph.source}:{inst.region}", ph.bytes / 1e9 / ph.seconds)
            await s.execute(
                update(Instance).where(Instance.id == inst.id).values(
                    models_loaded=[job.model_id], last_contact_at=utcnow()
                )
            )
        await self._release_instance(inst.id)

    # ------------------------------------------------------------------ helpers

    async def _cancelled(self, job_id: uuid.UUID) -> bool:
        async with self.app.sessionmaker() as s:
            return bool((await s.execute(select(Job.cancel_requested).where(Job.id == job_id))).scalar_one_or_none())

    async def _cancel(self, job: Job, inst: Instance, client: WorkerClient | None) -> None:
        if client is not None:
            try:
                await client.cancel(str(job.id))
                await client.delete_job(str(job.id))
            except WorkerUnreachable:
                pass
        async with self.app.sessionmaker() as s, s.begin():
            await transition_job(s, job.id, ("assigned", "preparing", "running"), "cancelled", stage="cancelled")
        await self._release_instance(inst.id)

    async def _release_instance(self, inst_id: uuid.UUID) -> None:
        """busy -> ready (idle clock starts only now, after outputs are safe)."""
        async with self.app.sessionmaker() as s, s.begin():
            await transition_instance(s, inst_id, "busy", "ready", idle_since=utcnow())
        if self.app.controller is not None:
            self.app.controller.wake()

    async def _finish_failed(self, job_id: uuid.UUID, code: str, detail: str) -> None:
        msg = FRIENDLY_ERRORS.get(code, FRIENDLY_ERRORS["engine"])
        log.warning("job %s failed (%s): %s", job_id, code, detail)
        async with self.app.sessionmaker() as s, s.begin():
            await transition_job(
                s, job_id, ("assigned", "preparing", "running", "fetching"), "failed",
                error_code=code, error_message=msg, stage="failed",
            )

    async def _requeue_or_fail(
        self, job_id: uuid.UUID, code: str, *, bump_vram: float | None = None, lost_instance: bool = False
    ) -> None:
        async with self.app.sessionmaker() as s, s.begin():
            job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
            if job is None or job.state in ("succeeded", "failed", "cancelled"):
                return
            inst_id = job.instance_id
            if job.attempt + 1 < job.max_attempts:
                fields = {"instance_id": None, "attempt": job.attempt + 1, "stage": "queued", "progress": 0.0}
                if bump_vram:
                    fields["min_vram_gb"] = bump_vram + 1
                await transition_job(s, job.id, job.state, "queued", **fields)
            else:
                await transition_job(
                    s, job.id, job.state, "failed", error_code=code,
                    error_message=FRIENDLY_ERRORS.get(code, FRIENDLY_ERRORS["engine"]), stage="failed",
                )
            if lost_instance and inst_id:
                await transition_instance(
                    s, inst_id, ("ready", "busy", "draining"), "destroying",
                    outcome="failed", failure_reason="stopped responding", destroy_requested_at=utcnow(),
                )
                await emit(s, scope="instance", type="instance.lost", ref_id=inst_id)
        if self.app.controller is not None:
            self.app.controller.wake()

