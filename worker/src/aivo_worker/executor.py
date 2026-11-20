"""Single-slot job executor. Job inputs/outputs live under a tmpfs directory only."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from aivo_protocol.worker_api import JobIn, JobState, JobStatus, PhaseTiming
from aivo_worker.engines.base import Engine, EngineError
from aivo_worker.outputs import postprocess
from aivo_worker.weights import FileManager

log = logging.getLogger(__name__)


@dataclass
class JobRecord:
    job: JobIn
    status: JobStatus
    dir: Path
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    def touch(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()


class Executor:
    def __init__(self, engine: Engine, files: FileManager, job_root: Path):
        self.engine = engine
        self.files = files
        self.job_root = job_root
        self.inputs_dir = job_root / "inputs"
        self.jobs: dict[str, JobRecord] = {}
        self.draining = False
        self.timings: list[PhaseTiming] = []
        self._slot = asyncio.Lock()
        self.inputs_dir.mkdir(parents=True, exist_ok=True)

    @property
    def running_job(self) -> str | None:
        for jid, rec in self.jobs.items():
            if rec.status.state in (JobState.running, JobState.postprocessing):
                return jid
        return None

    @property
    def outputs_pending(self) -> int:
        return sum(1 for r in self.jobs.values() if r.status.state == JobState.succeeded)

    def submit(self, job: JobIn) -> JobStatus:
        existing = self.jobs.get(job.job_id)
        if existing is not None:  # idempotent
            return existing.status
        if self.draining:
            raise RuntimeError("draining")
        jdir = self.job_root / job.job_id
        (jdir / "out").mkdir(parents=True, exist_ok=True)
        rec = JobRecord(job=job, status=JobStatus(job_id=job.job_id, state=JobState.queued, stage="queued"), dir=jdir)
        self.jobs[job.job_id] = rec
        rec.task = asyncio.create_task(self._run(rec), name=f"job:{job.job_id}")
        return rec.status

    async def _run(self, rec: JobRecord) -> None:
        job = rec.job
        async with self._slot:
            if rec.status.state == JobState.cancelled:
                return
            missing = [d for d in job.required_files if d not in set(self.files.present())]
            if missing:
                self._fail(rec, f"model files not ready: {missing[:3]}", "missing_files")
                return
            rec.status.state, rec.status.stage = JobState.running, "starting"
            rec.touch()
            t0 = time.monotonic()

            def progress(p: float, stage: str) -> None:
                rec.status.progress = max(0.0, min(1.0, p))
                rec.status.stage = stage
                elapsed = time.monotonic() - t0
                rec.status.eta_s = elapsed / p - elapsed if p > 0.05 else None
                rec.touch()

            try:
                inputs_dir = rec.dir / "in"
                inputs_dir.mkdir(exist_ok=True)
                for name in job.inputs:
                    src = self.inputs_dir / name
                    if src.exists():
                        shutil.move(str(src), inputs_dir / name)
                paths = await asyncio.wait_for(
                    self.engine.run(job, inputs_dir, rec.dir / "out", progress), timeout=job.timeout_s
                )
                self.timings.append(
                    PhaseTiming(phase="inference", seconds=time.monotonic() - t0, model_id=job.model_id)
                )
                rec.status.state, rec.status.stage = JobState.postprocessing, "saving"
                rec.touch()
                rec.status.outputs = [await postprocess(p) for p in paths]
                rec.status.state, rec.status.progress, rec.status.stage = JobState.succeeded, 1.0, "done"
                rec.status.eta_s = 0
                rec.touch()
            except TimeoutError:
                await self.engine.cancel()
                self._fail(rec, "generation timed out", "timeout")
            except EngineError as e:
                if e.code == "cancelled":
                    rec.status.state, rec.status.stage, rec.status.error_code = (
                        JobState.cancelled, "cancelled", "cancelled")
                    rec.touch()
                else:
                    self._fail(rec, str(e), e.code)  # type: ignore[arg-type]
            except asyncio.CancelledError:
                rec.status.state, rec.status.stage = JobState.cancelled, "cancelled"
                rec.touch()
                raise
            except Exception as e:
                log.exception("job %s failed", job.job_id)
                self._fail(rec, f"{type(e).__name__}: {e}", "engine")
            finally:
                with contextlib.suppress(Exception):
                    shutil.rmtree(rec.dir / "in", ignore_errors=True)

    def _fail(self, rec: JobRecord, msg: str, code: str) -> None:
        rec.status.state = JobState.failed
        rec.status.stage = "failed"
        rec.status.error = msg[:500]
        rec.status.error_code = code if code in {"oom", "engine", "timeout", "missing_files", "bad_request"} else "engine"  # type: ignore[assignment]
        rec.touch()

    async def wait(self, job_id: str, timeout: float, since_state: str | None = None) -> JobStatus | None:
        rec = self.jobs.get(job_id)
        if rec is None:
            return None
        if rec.status.state.terminal or timeout <= 0:
            return rec.status
        ev = rec.changed
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        return rec.status

    async def cancel(self, job_id: str) -> JobStatus | None:
        rec = self.jobs.get(job_id)
        if rec is None:
            return None
        if rec.status.state == JobState.queued:
            rec.status.state, rec.status.stage = JobState.cancelled, "cancelled"
            rec.touch()
        elif rec.status.state == JobState.running:
            await self.engine.cancel()
        return rec.status

    def output_path(self, job_id: str, name: str) -> Path | None:
        rec = self.jobs.get(job_id)
        if rec is None or rec.status.state != JobState.succeeded:
            return None
        if name not in {o.name for o in rec.status.outputs}:
            return None
        return rec.dir / "out" / name

    def delete(self, job_id: str) -> bool:
        rec = self.jobs.pop(job_id, None)
        if rec is None:
            return False
        if rec.task and not rec.task.done():
            rec.task.cancel()
        shutil.rmtree(rec.dir, ignore_errors=True)
        return True
