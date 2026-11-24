"""Entry point of the worker image on RunPod Serverless: ``aivo-worker``.

RunPod starts this container on demand for the GPU type's queue endpoint and hands it jobs one at a time.
The endpoint's environment holds no secrets: each job brings its own one-time ticket.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from pathlib import Path

from aivo_protocol.worker_api import LogLine
from aivo_worker.logbuf import BufferHandler, LogBuffer
from aivo_worker.runtime import WorkerRuntime

# A RAM-backed job directory must hold a job's inputs and outputs (a few hundred MB at most).
MIN_RAM_JOB_DIR_BYTES = 2 * 1024**3
NETWORK_VOLUME = Path("/runpod-volume")


def _echo(line: LogLine) -> None:
    if line.source == "engine":  # ComfyUI output is already passed through to stdout
        return
    stamp = time.strftime("%H:%M:%S", time.localtime(line.ts))
    print(f"{stamp} [{line.source}] {line.text}", file=sys.stdout, flush=True)


def pick_job_dir(preferred: str, fallback: str = "/tmp/aivo/jobs") -> str:
    """Keep creations in RAM (``/dev/shm``) when it is big enough; a container's default 64 MB is not."""
    base = Path(preferred)
    probe = next((p for p in (base, *base.parents) if p.exists()), None)
    try:
        if probe is not None and shutil.disk_usage(probe).total >= MIN_RAM_JOB_DIR_BYTES:
            return preferred
    except OSError:
        pass
    print(f"aivo: {preferred} is too small for jobs; using {fallback} (container disk)", file=sys.stderr)
    return fallback


def default_models_dir() -> str:
    """Model files outlive the worker on a network volume; otherwise they sit on the container disk."""
    if NETWORK_VOLUME.is_dir():
        return str(NETWORK_VOLUME / "aivo" / "models")
    return "/var/lib/aivo/models"


def build_runtime(logs: LogBuffer) -> WorkerRuntime:
    models_dir = Path(os.environ.get("AIVO_MODELS_DIR") or default_models_dir())
    job_dir = Path(pick_job_dir(os.environ.get("AIVO_JOB_DIR") or "/dev/shm/aivo/jobs"))
    if os.environ.get("AIVO_ENGINE", "comfy") == "mock":
        from aivo_worker.engines.mock import MockEngine

        engine = MockEngine(float(os.environ.get("AIVO_MOCK_JOB_SECONDS", "2")))
        return WorkerRuntime(engine, models_dir=models_dir, job_dir=job_dir, logs=logs, simulate_files=True)
    from aivo_worker.engines.comfy import ComfyEngine

    engine = ComfyEngine.from_env(models_dir, job_dir)
    return WorkerRuntime(engine, models_dir=models_dir, job_dir=job_dir, logs=logs)


def main() -> None:
    logging.basicConfig(level=os.environ.get("AIVO_LOG_LEVEL", "INFO"))
    logs = LogBuffer(echo=_echo)
    handler = BufferHandler(logs)
    handler.setLevel(logging.INFO)
    logging.getLogger("aivo_worker").addHandler(handler)
    runtime = build_runtime(logs)

    import runpod  # only in the worker image (see worker/Dockerfile)

    runpod.serverless.start({"handler": runtime.handle})


if __name__ == "__main__":
    main()
