"""Entry point on a real GPU machine: ``aivo-worker`` (systemd / docker)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from aivo_protocol.worker_api import WorkerBootstrap
from aivo_worker.server import WorkerServer, build_state
from aivo_worker.watchdog import Watchdog

BOOTSTRAP_PATH = Path(os.environ.get("AIVO_WORKER_BOOTSTRAP", "/etc/aivo/bootstrap.json"))


def load_bootstrap(path: Path = BOOTSTRAP_PATH) -> WorkerBootstrap:
    return WorkerBootstrap.model_validate(json.loads(path.read_text()))


async def amain() -> None:
    logging.basicConfig(level=os.environ.get("AIVO_LOG_LEVEL", "INFO"))
    boot = load_bootstrap()
    if boot.engine == "comfy":
        from aivo_worker.engines.comfy import ComfyEngine

        engine = ComfyEngine.from_env(boot)
    else:
        from aivo_worker.engines.mock import MockEngine

        engine = MockEngine(float(os.environ.get("AIVO_MOCK_JOB_SECONDS", "2")))
    state = build_state(boot, engine, simulate_files=boot.engine == "mock")
    server = WorkerServer(state, host=boot.listen_host, port=boot.listen_port)
    tasks = [asyncio.create_task(server.serve_forever())]
    if boot.watchdog.enabled:
        tasks.append(asyncio.create_task(Watchdog(boot.watchdog, state.auth).run()))
    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
