"""Run the worker agent with TLS. Used by ``main`` (real machines) and by the
backend's fake provider (in-process mock workers)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import tempfile
from pathlib import Path

import uvicorn

from aivo_protocol.worker_api import WorkerBootstrap
from aivo_worker.api import WorkerState, create_app, ensure_dirs
from aivo_worker.engines.base import Engine
from aivo_worker.executor import Executor
from aivo_worker.logbuf import LogBuffer
from aivo_worker.weights import FileManager


def build_state(
    boot: WorkerBootstrap,
    engine: Engine,
    *,
    simulate_files: bool = False,
    logs: LogBuffer | None = None,
    setup_log: Path | None = None,
) -> WorkerState:
    ensure_dirs(boot)
    logs = logs or LogBuffer()
    if setup_log is not None:
        logs.load_file(setup_log, "setup")  # start-up history (mounted from the host)
    files = FileManager(
        Path(boot.models_dir),
        simulate=simulate_files,
        volume_mount=Path(boot.volume_mount) if boot.volume_mount else None,
        logs=logs,
    )
    executor = Executor(engine, files, Path(boot.job_dir), logs=logs)
    if hasattr(engine, "logs"):
        engine.logs = logs
    return WorkerState(boot, engine, files, executor, logs)


class WorkerServer:
    def __init__(self, state: WorkerState, host: str = "127.0.0.1", port: int = 0):
        self.state = state
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._tlsdir: tempfile.TemporaryDirectory[str] | None = None

    def _bind(self) -> socket.socket:
        fam = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(fam, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        self.port = sock.getsockname()[1]
        return sock

    async def start(self) -> None:
        boot = self.state.boot
        # uvicorn needs files; keep them on tmpfs where available
        base = "/dev/shm" if os.path.isdir("/dev/shm") else None
        self._tlsdir = tempfile.TemporaryDirectory(prefix="aivo-tls-", dir=base)
        cert = Path(self._tlsdir.name) / "cert.pem"
        key = Path(self._tlsdir.name) / "key.pem"
        cert.write_text(boot.tls_cert_pem)
        key.write_text(boot.tls_key_pem)
        os.chmod(key, 0o600)
        self._sock = self._bind()
        config = uvicorn.Config(
            create_app(self.state),
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
            log_level="warning",
            lifespan="on",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[self._sock]), name="worker-server")
        for _ in range(200):
            if self._server.started:
                return
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.01)
        raise RuntimeError("worker server did not start")

    async def serve_forever(self) -> None:
        await self.start()
        assert self._task is not None
        await self._task

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=5)
        if self._sock:
            self._sock.close()
        if self._tlsdir:
            self._tlsdir.cleanup()
