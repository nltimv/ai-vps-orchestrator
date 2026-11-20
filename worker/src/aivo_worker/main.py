"""Entry point on a real GPU machine: ``aivo-worker`` (systemd / docker)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from aivo_protocol.worker_api import LogLine, WorkerBootstrap
from aivo_worker.logbuf import BufferHandler, LogBuffer
from aivo_worker.server import WorkerServer, build_state
from aivo_worker.watchdog import Watchdog

BOOTSTRAP_PATH = Path(os.environ.get("AIVO_WORKER_BOOTSTRAP", "/etc/aivo/bootstrap.json"))
# The machine's start-up log, mounted read-only into the container by bootstrap.sh.
SETUP_LOG = Path(os.environ.get("AIVO_SETUP_LOG", "/var/log/aivo-bootstrap.log"))


def _echo(line: LogLine) -> None:
    if line.source == "engine":  # ComfyUI output is already passed through to stdout
        return
    stamp = time.strftime("%H:%M:%S", time.localtime(line.ts))
    print(f"{stamp} [{line.source}] {line.text}", file=sys.stdout, flush=True)


def load_bootstrap(path: Path = BOOTSTRAP_PATH) -> WorkerBootstrap:
    return WorkerBootstrap.model_validate(json.loads(path.read_text()))


async def amain() -> None:
    logging.basicConfig(level=os.environ.get("AIVO_LOG_LEVEL", "INFO"))
    logs = LogBuffer(echo=_echo)
    handler = BufferHandler(logs)
    handler.setLevel(logging.INFO)
    logging.getLogger("aivo_worker").addHandler(handler)
    boot = load_bootstrap()
    if boot.engine == "comfy":
        from aivo_worker.engines.comfy import ComfyEngine

        engine = ComfyEngine.from_env(boot)
    else:
        from aivo_worker.engines.mock import MockEngine

        engine = MockEngine(float(os.environ.get("AIVO_MOCK_JOB_SECONDS", "2")))
    state = build_state(
        boot,
        engine,
        simulate_files=boot.engine == "mock",
        logs=logs,
        setup_log=SETUP_LOG if SETUP_LOG.is_file() else None,
    )
    server = WorkerServer(state, host=boot.listen_host, port=boot.listen_port)
    tasks = [asyncio.create_task(server.serve_forever())]
    if boot.watchdog.enabled:
        tasks.append(asyncio.create_task(Watchdog(boot.watchdog, state.auth).run()))
    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
