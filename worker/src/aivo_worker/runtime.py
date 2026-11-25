"""One worker process: RunPod hands it one job at a time from the endpoint's queue.

The job's input is only a ticket. With it, the worker asks aivo what to make, downloads the model files
(kept on disk for the next job on this worker), fetches the starting picture, runs the AI engine, reports
progress and log lines, uploads the results and says it's done. RunPod's job system only ever sees the
ticket and a list of file names.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aivo_protocol import protocol_compatible
from aivo_protocol.worker_api import (
    CompleteIn,
    ErrorCode,
    FailIn,
    JobIn,
    JobState,
    LogLine,
    ProgressIn,
    ProgressOut,
    Ticket,
    WorkerInfo,
    WorkSpec,
)
from aivo_worker import __version__
from aivo_worker.aivo_client import AivoClient, TicketRejected
from aivo_worker.engines.base import Engine
from aivo_worker.executor import Executor
from aivo_worker.logbuf import LogBuffer
from aivo_worker.weights import FileManager

log = logging.getLogger(__name__)


def _short(job_id: str) -> str:
    return job_id.split("-", 1)[0][:8]


class WorkerRuntime:
    def __init__(
        self,
        engine: Engine,
        *,
        models_dir: Path,
        job_dir: Path,
        logs: LogBuffer | None = None,
        simulate_files: bool = False,
        transport: Any = None,  # httpx transport to aivo (tests / the simulated cloud)
        worker_id: str | None = None,
        endpoint_id: str | None = None,
        progress_every_s: float = 2.0,
    ):
        self.logs = logs or LogBuffer()
        self.engine = engine
        if hasattr(engine, "logs"):
            engine.logs = self.logs
        models_dir.mkdir(parents=True, exist_ok=True)
        job_dir.mkdir(parents=True, exist_ok=True)
        self.files = FileManager(models_dir, simulate=simulate_files, logs=self.logs)
        self.executor = Executor(engine, self.files, job_dir, logs=self.logs)
        self.transport = transport
        self.worker_id = worker_id or os.environ.get("RUNPOD_POD_ID") or socket.gethostname()
        self.endpoint_id = endpoint_id or os.environ.get("RUNPOD_ENDPOINT_ID")
        self.progress_every_s = progress_every_s
        self.started_at = time.time()
        self.prev_job_end: float | None = None
        self.jobs_done = 0
        self.engine_error: str | None = None
        self._engine_task: asyncio.Task[None] | None = None
        self._sent_seq = 0

    # -- helpers ----------------------------------------------------------------------

    def info(self, job_started_at: float | None = None) -> WorkerInfo:
        return WorkerInfo(
            worker_id=self.worker_id,
            endpoint_id=self.endpoint_id,
            started_at=self.started_at,
            job_started_at=job_started_at,
            prev_job_end=self.prev_job_end,
            jobs_done=self.jobs_done,
            gpu=self.engine.gpu(),
            agent_version=__version__,
        )

    def _start_engine(self) -> None:
        """The AI engine starts with the first job, while the model files download."""
        if self._engine_task is not None:
            return

        async def start() -> None:
            self.logs.emit("agent", f"worker {__version__} up; starting the AI engine ({self.engine.name})")
            t0 = time.monotonic()
            try:
                await self.engine.start()
                self.logs.emit("agent", f"AI engine ready after {time.monotonic() - t0:.0f} s")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.engine_error = f"{type(e).__name__}: {e}"[:500]
                self.logs.emit("agent", f"AI engine failed to start: {self.engine_error}", "error")

        self._engine_task = asyncio.create_task(start(), name="engine-start")

    def _new_lines(self) -> list[LogLine]:
        out = self.logs.read(since=self._sent_seq, limit=400)
        self._sent_seq = out.next_seq
        return out.lines

    async def _push(self, aivo: AivoClient, stage: str, progress: float, eta_s: float | None = None) -> ProgressOut:
        return await aivo.progress(ProgressIn(stage=stage, progress=progress, eta_s=eta_s, lines=self._new_lines()))

    async def _fail(self, aivo: AivoClient, code: ErrorCode, message: str, t0: float) -> dict[str, Any]:
        self.logs.emit("agent", f"job {_short(aivo.ticket.job_id)}: {code}: {message[:300]}", "error")
        await aivo.fail(
            FailIn(
                code=code,
                message=message[:2000],
                worker=self.info(t0),
                finished_at=time.time(),
                lines=self._new_lines(),
            )
        )
        if code == "cancelled":
            return {"cancelled": True}
        return {"error": f"{code}: {message[:500]}"}

    # -- the RunPod handler -----------------------------------------------------------

    async def handle(self, job: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        try:
            ticket = Ticket.model_validate(job.get("input") or {})
        except ValidationError:
            return {"error": "bad_request: the job input is not an aivo ticket"}
        if not protocol_compatible(ticket.protocol):
            return {"error": f"bad_request: aivo speaks protocol {ticket.protocol}, this worker another"}
        try:
            async with AivoClient(ticket, transport=self.transport) as aivo:
                try:
                    return await self._work(aivo, ticket, t0)
                except TicketRejected as e:
                    self.logs.emit("agent", f"job {_short(ticket.job_id)}: aivo no longer wants it ({e})")
                    return {"skipped": True}
                except Exception as e:  # anything unexpected: tell aivo, then let RunPod record it
                    log.exception("job %s failed", ticket.job_id)
                    with contextlib.suppress(Exception):
                        return await self._fail(aivo, "engine", f"{type(e).__name__}: {e}", t0)
                    return {"error": f"engine: {type(e).__name__}: {e}"}
        finally:
            self.prev_job_end = time.time()
            self.jobs_done += 1

    async def _work(self, aivo: AivoClient, ticket: Ticket, t0: float) -> dict[str, Any]:
        self._start_engine()
        spec: WorkSpec = await aivo.spec(self.info(t0))
        sid = _short(spec.job_id)
        self.logs.emit("agent", f"job {sid}: {spec.model_id}, {len(spec.files)} model file(s)")

        # 1) model files (already here when this worker ran the same engine before)
        dests = [f.dest for f in spec.files]
        self.files.ensure(spec.files)
        total = sum(f.bytes or 0 for f in spec.files) or 1
        while True:
            st = self.files.status(dests)
            if st.all_ready:
                break
            if st.failed:
                return await self._fail(
                    aivo, "missing_files", "; ".join(f"{f.dest}: {f.error}" for f in st.failed)[:1500], t0
                )
            done = sum(f.bytes_done for f in st.files)
            if (await self._push(aivo, "loading_model", min(0.99, done / total))).cancel:
                return await self._fail(aivo, "cancelled", "cancelled while loading the model", t0)
            await asyncio.sleep(self.progress_every_s)

        # 2) the AI engine
        if self._engine_task is not None:
            await self._engine_task
        if self.engine_error:
            return await self._fail(aivo, "engine", f"The AI engine could not start: {self.engine_error}", t0)

        # 3) starting files, then the job itself
        for name in spec.inputs:
            await aivo.get_input(name, self.executor.inputs_dir / name)
        self.executor.submit(
            JobIn(
                job_id=spec.job_id,
                model_id=spec.model_id,
                workflow=spec.workflow,
                inputs=spec.inputs,
                required_files=dests,
                output=spec.output,
                timeout_s=spec.timeout_s,
            )
        )
        while True:
            status = await self.executor.wait(spec.job_id, self.progress_every_s)
            if status is None:
                return await self._fail(aivo, "engine", "the job disappeared from the worker", t0)
            if status.state.terminal:
                break
            stage = {"loading_model": "loading_model", "saving": "saving"}.get(status.stage, "generating")
            if (await self._push(aivo, stage, status.progress, status.eta_s)).cancel:
                await self.executor.cancel(spec.job_id)
        if status.state != JobState.succeeded:
            self.executor.delete(spec.job_id)
            code = "cancelled" if status.state == JobState.cancelled else (status.error_code or "engine")
            return await self._fail(aivo, code, status.error or "the AI engine failed", t0)

        # 4) results go straight to aivo, then nothing of them stays here
        await self._push(aivo, "saving", 1.0)
        t_up = time.monotonic()
        try:
            for o in status.outputs:
                path = self.executor.output_path(spec.job_id, o.name)
                if path is None:
                    raise RuntimeError(f"output {o.name} is missing")
                await aivo.upload(o.name, path, o.media_type)
            phases = [*self.files.timings, *self.executor.timings]
            self.files.timings, self.executor.timings = [], []
            size = sum(o.bytes for o in status.outputs) / 1e6
            self.logs.emit("agent", f"job {sid}: {size:.1f} MB delivered in {time.monotonic() - t_up:.1f} s")
            await aivo.complete(
                CompleteIn(
                    outputs=status.outputs,
                    phases=phases,
                    worker=self.info(t0),
                    finished_at=time.time(),
                    lines=self._new_lines(),
                )
            )
        finally:
            self.executor.delete(spec.job_id)
        return {"outputs": [o.name for o in status.outputs]}
