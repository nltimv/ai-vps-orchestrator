"""A simulated RunPod Serverless, in this process (AIVO_PROVIDER=fake: development, CI and tests).

It behaves like a queue-based endpoint: jobs wait in the endpoint's queue, workers start on demand (up to the
endpoint's max workers, after a cold start), take one job at a time, wait ``idleTimeout`` for the next one and
then scale down. The workers run the real worker code (``aivo_worker.runtime``) with the CPU mock engine, so
they fetch the job from aivo, report progress and upload results exactly like a GPU worker does.

Chaos (``AIVO_FAKE_CHAOS`` or ``FakeRunPod.chaos``), each entry used once:
* ``no_gpu``        - the next endpoint gets no workers (its jobs wait in the queue)
* ``worker_crash``  - the next job's worker dies mid-job (RunPod retries the job once on another worker)
* ``oom`` / ``engine_fail`` - the next job fails in the AI engine
* ``rewrite_fail`` / ``rewrite_refused`` / ``rewrite_minor`` - for the next job, the language model that
  improves the description errors, declines, or writes about a teenager
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aivo.providers.base import ConfigError, QuotaError

CATALOG = [
    ("NVIDIA RTX 4000 Ada Generation", "RTX 4000 Ada", "AMPERE_16", 20, 0.58, 40),
    ("NVIDIA RTX A5000", "RTX A5000", "AMPERE_24", 24, 0.68, 46),
    ("NVIDIA L4", "L4", "AMPERE_24", 24, 0.68, 50),
    ("NVIDIA L40S", "L40S", "ADA_48_PRO", 48, 1.91, 94),
    ("NVIDIA RTX 6000 Ada Generation", "RTX 6000 Ada", "ADA_48_PRO", 48, 1.91, 167),
    ("NVIDIA H100 80GB HBM3", "H100 SXM", "ADA_80_PRO", 80, 4.18, 188),
]
_ids = itertools.count(1)


@dataclass
class FakeJob:
    id: str
    input: dict[str, Any]
    policy: dict[str, Any]
    status: str = "IN_QUEUE"
    output: Any = None
    error: str | None = None
    submitted: float = field(default_factory=time.monotonic)
    started: float | None = None
    finished: float | None = None
    retries: int = 0
    chaos: str | None = None


@dataclass
class FakeWorker:
    id: str
    runtime: Any
    ready_at: float
    last_job_end: float
    job: FakeJob | None = None
    task: asyncio.Task[None] | None = None


@dataclass
class FakeEndpoint:
    body: dict[str, Any]
    id: str
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    jobs: list[FakeJob] = field(default_factory=list)
    workers: list[FakeWorker] = field(default_factory=list)
    no_gpu: bool = False

    def as_json(self) -> dict[str, Any]:
        return {**self.body, "id": self.id, "createdAt": self.created_at}


class FakeRunPod:
    name = "fake"

    def __init__(
        self,
        *,
        boot_seconds: float = 1.0,
        job_seconds: float = 1.0,
        chaos: list[str] | None = None,
        max_workers_total: int = 5,
        flashboot_limit: int | None = None,
        tick_s: float = 0.05,
        progress_every_s: float = 0.2,
    ):
        self.boot_seconds = boot_seconds
        self.job_seconds = job_seconds
        self.chaos = list(chaos or [])
        self.workflows: dict[str, Any] = {}  # aivo job id -> the workflow its worker ran (for tests)
        self.max_workers_total = max_workers_total
        self.flashboot_limit = flashboot_limit  # like RunPod plans: FlashBoot on at most this many endpoints
        self.tick_s = tick_s
        self.progress_every_s = progress_every_s
        self.endpoints: dict[str, FakeEndpoint] = {}
        self.deleted: list[str] = []
        self.transport: Any = None  # httpx transport to aivo, set by attach()
        self._root = Path(tempfile.mkdtemp(prefix="aivo-fake-runpod-"))
        self._loop: asyncio.Task[None] | None = None

    def attach(self, transport: Any) -> None:
        """Workers reach aivo through this transport (in-process: httpx.ASGITransport of the app)."""
        self.transport = transport

    def _take_chaos(self, *kinds: str) -> str | None:
        for i, c in enumerate(self.chaos):
            if c in kinds:
                return self.chaos.pop(i)
        return None

    def _ensure_loop(self) -> None:
        if self._loop is None or self._loop.done():
            self._loop = asyncio.create_task(self._run(), name="fake-runpod")

    async def aclose(self) -> None:
        if self._loop:
            self._loop.cancel()
            with contextlib.suppress(BaseException):
                await self._loop
        for ep in list(self.endpoints.values()):
            await self._stop_workers(ep)

    # -- REST API v2 -----------------------------------------------------------------

    async def gpu_types(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": tid,
                "name": name,
                "pool": pool,
                "manufacturer": "NVIDIA",
                "memory": mem,
                "secure": True,
                "community": False,
                "price": {"secure": round(price / 1.7, 2), "community": 0, "serverless": price},
                "maxCount": {"secure": 8, "community": 0},
                "minRam": ram,
                "availability": "HIGH",
                "dataCenters": [{"id": "EU-NL-1", "name": "Netherlands 1", "availability": "HIGH"}],
            }
            for tid, name, pool, mem, price, ram in CATALOG
        ]

    async def network_volume(self, volume_id: str) -> dict[str, Any] | None:
        return {"id": volume_id, "dataCenter": "EU-NL-1", "size": 200}

    async def list_endpoints(self) -> list[dict[str, Any]]:
        return [e.as_json() for e in self.endpoints.values()]

    async def get_endpoint(self, endpoint_id: str) -> dict[str, Any] | None:
        ep = self.endpoints.get(endpoint_id)
        return ep.as_json() if ep else None

    async def create_endpoint(self, body: dict[str, Any]) -> dict[str, Any]:
        assert body["type"] == "QUEUE"
        used = sum(e.body["workers"]["max"] for e in self.endpoints.values())
        if used + body["workers"]["max"] > self.max_workers_total:
            raise QuotaError("HTTP 400: max workers limit exceeded for your account", http_status=400)
        flashbooted = sum(1 for e in self.endpoints.values() if e.body.get("flashboot") == "FLASHBOOT")
        if (
            self.flashboot_limit is not None
            and body.get("flashboot") == "FLASHBOOT"
            and flashbooted >= self.flashboot_limit
        ):
            raise ConfigError(
                f"HTTP 400: This account's plan allows {self.flashboot_limit} endpoints with FlashBoot; "
                "turn it off for this endpoint or upgrade the plan."
            )
        ep = FakeEndpoint(body=dict(body), id=f"fake{next(_ids):04d}")
        ep.no_gpu = self._take_chaos("no_gpu") is not None
        self.endpoints[ep.id] = ep
        return ep.as_json()

    async def update_endpoint(self, endpoint_id: str, body: dict[str, Any]) -> dict[str, Any]:
        ep = self.endpoints[endpoint_id]
        for k, v in body.items():
            ep.body[k] = {**ep.body.get(k, {}), **v} if isinstance(v, dict) and k == "workers" else v
        return ep.as_json()

    async def delete_endpoint(self, endpoint_id: str) -> None:
        ep = self.endpoints.pop(endpoint_id, None)
        if ep is not None:
            await self._stop_workers(ep)
            self.deleted.append(endpoint_id)

    # -- queue -----------------------------------------------------------------------

    async def run(self, endpoint_id: str, job_input: dict[str, Any], policy: dict[str, Any]) -> str:
        ep = self.endpoints[endpoint_id]
        job = FakeJob(id=f"job-{secrets.token_hex(6)}", input=job_input, policy=policy)
        job.chaos = self._take_chaos(
            "worker_crash", "oom", "engine_fail", "rewrite_fail", "rewrite_refused", "rewrite_minor"
        )
        ep.jobs.append(job)
        self._ensure_loop()
        return job.id

    def _find(self, endpoint_id: str, job_id: str) -> FakeJob | None:
        ep = self.endpoints.get(endpoint_id)
        return next((j for j in ep.jobs if j.id == job_id), None) if ep else None

    async def status(self, endpoint_id: str, job_id: str) -> dict[str, Any] | None:
        j = self._find(endpoint_id, job_id)
        if j is None:
            return None
        now = time.monotonic()
        out: dict[str, Any] = {"id": j.id, "status": j.status}
        if j.started is not None:
            out["delayTime"] = int((j.started - j.submitted) * 1000)
            out["executionTime"] = int(((j.finished or now) - j.started) * 1000)
        if j.output is not None:
            out["output"] = j.output
        if j.error:
            out["error"] = j.error
        return out

    async def cancel(self, endpoint_id: str, job_id: str) -> None:
        j = self._find(endpoint_id, job_id)
        if j is None or j.status not in ("IN_QUEUE", "IN_PROGRESS"):
            return
        ep = self.endpoints[endpoint_id]
        for w in ep.workers:
            if w.job is j and w.task and not w.task.done():
                w.task.cancel()
        j.status, j.finished = "CANCELLED", time.monotonic()

    async def health(self, endpoint_id: str) -> dict[str, Any]:
        ep = self.endpoints.get(endpoint_id)
        if ep is None:
            return {}
        now = time.monotonic()
        return {
            "jobs": {
                "inQueue": sum(1 for j in ep.jobs if j.status == "IN_QUEUE"),
                "inProgress": sum(1 for j in ep.jobs if j.status == "IN_PROGRESS"),
                "completed": sum(1 for j in ep.jobs if j.status == "COMPLETED"),
                "failed": sum(1 for j in ep.jobs if j.status == "FAILED"),
            },
            "workers": {
                "running": sum(1 for w in ep.workers if w.job is not None),
                "idle": sum(1 for w in ep.workers if w.job is None and w.ready_at <= now),
                "initializing": sum(1 for w in ep.workers if w.ready_at > now),
            },
        }

    # -- the simulation --------------------------------------------------------------

    def _new_worker(self, ep: FakeEndpoint) -> FakeWorker:
        from aivo_worker.engines.mock import MockEngine
        from aivo_worker.runtime import WorkerRuntime

        wid = f"wrk-{secrets.token_hex(4)}"
        root = self._root / ep.id / wid
        runtime = WorkerRuntime(
            MockEngine(self.job_seconds, seen=self.workflows),
            models_dir=root / "models",
            job_dir=root / "jobs",
            simulate_files=True,
            transport=self.transport,
            worker_id=wid,
            endpoint_id=ep.id,
            progress_every_s=self.progress_every_s,
        )
        now = time.monotonic()
        return FakeWorker(id=wid, runtime=runtime, ready_at=now + self.boot_seconds, last_job_end=now)

    async def _run(self) -> None:
        while True:
            now = time.monotonic()
            for ep in list(self.endpoints.values()):
                self._tick(ep, now)
            await asyncio.sleep(self.tick_s)

    def _tick(self, ep: FakeEndpoint, now: float) -> None:
        workers = ep.body.get("workers") or {}
        idle_timeout = float(workers.get("idleTimeout", 5))
        # queued jobs that waited too long expire (the job's ttl)
        for j in ep.jobs:
            ttl = (j.policy.get("ttl") or 86_400_000) / 1000
            if j.status == "IN_QUEUE" and now - j.submitted > ttl:
                j.status, j.finished = "TIMED_OUT", now
        queued = [j for j in ep.jobs if j.status == "IN_QUEUE"]
        # scale down idle workers (keep the endpoint's minimum)
        for w in list(ep.workers):
            if w.job is None and w.ready_at <= now and now - w.last_job_end > idle_timeout:
                if len(ep.workers) > int(workers.get("min", 0)):
                    ep.workers.remove(w)
        # start workers for waiting jobs, up to the endpoint's max
        if not ep.no_gpu:
            starting = sum(1 for w in ep.workers if w.job is None)
            want = min(int(workers.get("max", 1)), len(ep.workers) + max(0, len(queued) - starting))
            want = max(want, int(workers.get("min", 0)))
            while len(ep.workers) < want:
                ep.workers.append(self._new_worker(ep))
        # ready, idle workers take the oldest job
        for w in ep.workers:
            if not queued:
                break
            if w.job is None and w.ready_at <= now:
                job = queued.pop(0)
                w.job, job.status, job.started = job, "IN_PROGRESS", now
                w.task = asyncio.create_task(self._work(ep, w, job), name=f"fake-worker:{w.id}")

    async def _work(self, ep: FakeEndpoint, w: FakeWorker, job: FakeJob) -> None:
        chaos, job.chaos = job.chaos, None
        engine = w.runtime.engine
        if chaos in ("oom", "engine_fail"):
            engine.fail_next = "oom" if chaos == "oom" else "engine"
        engine.rewrite_fails = chaos == "rewrite_fail"
        engine.rewrite_reply = {
            "rewrite_refused": "I'm sorry, but I can't help with that request.",
            "rewrite_minor": "A portrait of a teenager on a beach at golden hour, soft light, film grain.",
        }.get(chaos or "")
        try:
            if chaos == "worker_crash":
                handle = asyncio.create_task(w.runtime.handle({"id": job.id, "input": job.input}))
                await asyncio.sleep(max(0.3, self.job_seconds / 2))
                handle.cancel()
                with contextlib.suppress(BaseException):
                    await handle
                raise ConnectionAbortedError("worker died")
            result = await w.runtime.handle({"id": job.id, "input": job.input})
        except asyncio.CancelledError:
            if job.status == "IN_PROGRESS":
                job.status, job.finished = "CANCELLED", time.monotonic()
            w.job, w.last_job_end = None, time.monotonic()
            return
        except Exception as e:
            # the worker is gone; RunPod retries the job once on another worker
            if w in ep.workers:
                ep.workers.remove(w)
            if job.retries < 1:
                job.retries += 1
                job.status, job.started = "IN_QUEUE", None
            else:
                job.status, job.error, job.finished = "FAILED", f"worker exited: {e}", time.monotonic()
            return
        job.finished = time.monotonic()
        if isinstance(result, dict) and result.get("error"):
            job.status, job.error = "FAILED", str(result["error"])
        else:
            job.status, job.output = "COMPLETED", result
        w.job, w.last_job_end = None, time.monotonic()

    async def _stop_workers(self, ep: FakeEndpoint) -> None:
        for w in ep.workers:
            if w.task and not w.task.done():
                w.task.cancel()
                with contextlib.suppress(BaseException):
                    await w.task
        ep.workers.clear()

    # helpers for tests
    def workers_of(self, endpoint_id: str) -> list[FakeWorker]:
        ep = self.endpoints.get(endpoint_id)
        return list(ep.workers) if ep else []
