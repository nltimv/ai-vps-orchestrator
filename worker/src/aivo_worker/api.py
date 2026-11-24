"""Worker agent HTTP API (served over TLS with a per-machine certificate, or behind RunPod's gateway)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from aivo_protocol.version import PROTOCOL_VERSION
from aivo_protocol.worker_api import (
    EnsureFilesIn,
    FilesStatus,
    Health,
    JobIn,
    JobStatus,
    LogsOut,
    RotateTokenOut,
    TelemetryOut,
    WorkerBootstrap,
)
from aivo_worker import __version__
from aivo_worker.engines.base import Engine
from aivo_worker.executor import Executor
from aivo_worker.logbuf import LogBuffer
from aivo_worker.security import TokenAuth
from aivo_worker.weights import FileManager

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class WorkerState:
    def __init__(
        self,
        boot: WorkerBootstrap,
        engine: Engine,
        files: FileManager,
        executor: Executor,
        logs: LogBuffer | None = None,
    ):
        self.boot = boot
        self.engine = engine
        self.files = files
        self.executor = executor
        self.logs = logs or LogBuffer()
        self.auth = TokenAuth(boot.token_sha256)
        self.started = time.monotonic()
        self.engine_error: str | None = None
        self._engine_task: asyncio.Task[None] | None = None

    async def start_engine(self) -> None:
        """Start the AI engine in the background so health and logs are served meanwhile."""
        self.logs.emit("agent", f"agent {__version__} up; starting the AI engine ({self.engine.name})")
        t0 = time.monotonic()
        try:
            await self.engine.start()
            self.logs.emit("agent", f"AI engine ready after {time.monotonic() - t0:.0f} s")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.engine_error = f"{type(e).__name__}: {e}"[:500]
            self.logs.emit("agent", f"AI engine failed to start: {self.engine_error}", "error")


def create_app(state: WorkerState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        state._engine_task = asyncio.create_task(state.start_engine(), name="engine-start")
        try:
            yield
        finally:
            if state._engine_task and not state._engine_task.done():
                state._engine_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await state._engine_task
            await state.engine.stop()

    app = FastAPI(title="aivo worker", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

    def auth(request: Request) -> None:
        state.auth.check(request)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        """Health check of RunPod's load balancer (no token: it says nothing but "up")."""
        return {"status": "ok"}

    @app.get("/v1/health", dependencies=[Depends(auth)])
    async def health() -> Health:
        disk = None
        try:
            disk = shutil.disk_usage(state.files.models_dir).free / 1e9
        except OSError:
            pass
        return Health(
            protocol=PROTOCOL_VERSION,
            agent_version=__version__,
            instance_id=state.boot.instance_id,
            engine=state.engine.name,  # type: ignore[arg-type]
            engine_ready=state.engine.ready,
            gpu=state.engine.gpu(),
            disk_free_gb=disk,
            files_present=state.files.present(),
            models_loaded=state.engine.models_loaded,
            running_job=state.executor.running_job,
            outputs_pending=state.executor.outputs_pending,
            draining=state.executor.draining,
            uptime_s=time.monotonic() - state.started,
            image_digest=state.boot.worker_image,
            engine_error=state.engine_error,
            log_seq=state.logs.last_seq,
        )

    @app.get("/v1/logs", dependencies=[Depends(auth)])
    async def logs(
        since: int = Query(0, ge=0),
        wait: float = Query(0, ge=0, le=30),
        limit: int = Query(1000, ge=1, le=5000),
        tail: int | None = Query(None, ge=0, le=5000),
    ) -> LogsOut:
        if tail is not None:
            return state.logs.read(tail=tail)
        return await state.logs.wait_read(since, wait, limit)

    @app.post("/v1/files/ensure", dependencies=[Depends(auth)])
    async def ensure_files(body: EnsureFilesIn) -> FilesStatus:
        return state.files.ensure(body.files)

    @app.get("/v1/files/status", dependencies=[Depends(auth)])
    async def files_status(dest: list[str] = Query(default=[])) -> FilesStatus:
        return state.files.status(dest or None)

    @app.put("/v1/inputs/{name}", dependencies=[Depends(auth)])
    async def put_input(name: str, request: Request) -> dict[str, int]:
        if not _SAFE_NAME.match(name):
            raise HTTPException(400, "bad input name")
        dest = state.executor.inputs_dir / name
        size = 0
        with dest.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > 200 * 1024 * 1024:
                    raise HTTPException(413, "input too large")
                fh.write(chunk)
        return {"bytes": size}

    @app.post("/v1/jobs", dependencies=[Depends(auth)])
    async def submit(job: JobIn) -> JobStatus:
        if not state.engine.ready:
            raise HTTPException(409, "the AI engine is not ready")
        try:
            return state.executor.submit(job)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(auth)])
    async def get_job(job_id: str, wait: float = Query(0, ge=0, le=30)) -> JobStatus:
        st = await state.executor.wait(job_id, wait)
        if st is None:
            raise HTTPException(404, "unknown job")
        return st

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(auth)])
    async def cancel(job_id: str) -> JobStatus:
        st = await state.executor.cancel(job_id)
        if st is None:
            raise HTTPException(404, "unknown job")
        return st

    @app.get("/v1/jobs/{job_id}/outputs/{name}", dependencies=[Depends(auth)])
    async def output(job_id: str, name: str, request: Request) -> Response:
        if not _SAFE_NAME.match(name):
            raise HTTPException(400, "bad name")
        path = state.executor.output_path(job_id, name)
        if path is None or not path.exists():
            raise HTTPException(404, "no such output")
        return _range_file(path, request)

    @app.delete("/v1/jobs/{job_id}", dependencies=[Depends(auth)])
    async def delete(job_id: str) -> dict[str, bool]:
        return {"deleted": state.executor.delete(job_id)}

    @app.post("/v1/drain", dependencies=[Depends(auth)])
    async def drain() -> Health:
        state.executor.draining = True
        return await health()

    @app.post("/v1/rotate-token", dependencies=[Depends(auth)])
    async def rotate() -> RotateTokenOut:
        return RotateTokenOut(token=state.auth.rotate())

    @app.get("/v1/telemetry", dependencies=[Depends(auth)])
    async def telemetry() -> TelemetryOut:
        phases = state.files.timings + state.executor.timings
        state.files.timings, state.executor.timings = [], []
        return TelemetryOut(phases=phases)

    return app


def _range_file(path: Path, request: Request) -> Response:
    size = path.stat().st_size
    rng = request.headers.get("range")
    start, end = 0, size - 1
    status = 200
    if rng and rng.startswith("bytes="):
        a, _, b = rng[6:].partition("-")
        try:
            start = int(a) if a else max(0, size - int(b))
            end = min(int(b), size - 1) if (a and b) else size - 1
        except ValueError:
            raise HTTPException(416, "bad range") from None
        if start > end or start >= size:
            raise HTTPException(416, "range not satisfiable")
        status = 206

    def gen():  # type: ignore[no-untyped-def]
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {"accept-ranges": "bytes", "content-length": str(end - start + 1)}
    if status == 206:
        headers["content-range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(gen(), status_code=status, headers=headers, media_type="application/octet-stream")


def ensure_dirs(boot: WorkerBootstrap) -> None:
    for d in (boot.job_dir, boot.models_dir):
        os.makedirs(d, exist_ok=True)
