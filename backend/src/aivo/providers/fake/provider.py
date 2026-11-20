"""Fake cloud provider for local development and tests.

Each "machine" is an in-process worker agent (mock engine) served over real TLS
on a random localhost port, so the backend exercises the exact same code paths
(bootstrap, pinned TLS, token, jobs, downloads) as with a real GPU droplet,
without spending money.

Failure injection (``chaos``): queue outcomes per ``"<size>:<region>"`` (or ``"*"``):
``capacity``, ``quota``, ``volume_busy``, ``unknown_created``, ``unknown_lost``,
``boot_hang``, ``engine_oom``, ``engine_fail``.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aivo.providers.base import (
    BootImage,
    CapacityError,
    GpuOffer,
    InstanceSpec,
    ProviderInstance,
    QuotaError,
    RegionMeta,
    UnknownOutcome,
    VolumeBusy,
)
from aivo.providers.regions import DO_REGIONS
from aivo_worker.engines.mock import MockEngine
from aivo_worker.server import WorkerServer, build_state

OFFERS_FILE = Path(__file__).with_name("offers.yaml")


@dataclass
class FakeMachine:
    pi: ProviderInstance
    spec: InstanceSpec
    server: WorkerServer | None = None
    boot_task: asyncio.Task[None] | None = None
    engine: MockEngine | None = None
    root: Path | None = None


@dataclass
class FakeProvider:
    boot_seconds: float = 1.0
    job_seconds: float = 1.0
    scratch_root: Path | None = None
    name: str = "fake"
    chaos: dict[str, list[str]] = field(default_factory=dict)
    sold_out: set[str] = field(default_factory=set)  # "<size>:<region>" hidden from offers
    machines: dict[str, FakeMachine] = field(default_factory=dict)
    firewall_cidrs: list[str] = field(default_factory=list)
    create_calls: int = 0
    _seq: int = 0

    def __post_init__(self) -> None:
        self._raw = yaml.safe_load(OFFERS_FILE.read_text())
        if self.scratch_root is None:
            base = "/dev/shm" if Path("/dev/shm").is_dir() else None
            self.scratch_root = Path(tempfile.mkdtemp(prefix="aivo-fake-", dir=base))

    # -- offers -------------------------------------------------------------------

    async def list_offers(self) -> list[GpuOffer]:
        out = []
        for s in self._raw:
            for region in s["regions"]:
                if f"{s['slug']}:{region}" in self.sold_out:
                    continue
                out.append(
                    GpuOffer(
                        provider=self.name, size_slug=s["slug"], region=region, gpu_model=s["label"],
                        gpu_class=s["model"], gpu_count=s["count"], vram_gb=s["vram"], vcpus=s["vcpus"],
                        ram_gb=s["ram"], disk_gb=s["disk"], scratch_gb=s["scratch"], price_hourly=s["price"],
                        vendor=s["vendor"],
                    )
                )
        return out

    def regions(self) -> dict[str, RegionMeta]:
        return DO_REGIONS

    async def boot_image(self, offer: GpuOffer, strategy: str) -> BootImage:
        return BootImage(ref="fake-snapshot" if strategy != "base" else "fake-base", kind="snapshot" if strategy != "base" else "base")

    # -- lifecycle ----------------------------------------------------------------

    def _pop_chaos(self, offer: GpuOffer) -> str | None:
        for key in (f"{offer.size_slug}:{offer.region}", "*"):
            q = self.chaos.get(key)
            if q:
                return q.pop(0)
        return None

    async def create_instance(self, spec: InstanceSpec) -> ProviderInstance:
        self.create_calls += 1
        outcome = self._pop_chaos(spec.offer)
        if outcome == "capacity":
            raise CapacityError("no capacity for this size in this region (fake)", http_status=422)
        if outcome == "quota":
            raise QuotaError("GPU droplet limit reached (fake)", http_status=422)
        if outcome == "volume_busy" and spec.volume_ids:
            raise VolumeBusy("volume is still attached to another droplet (fake)", http_status=422)
        if outcome == "unknown_lost":
            raise UnknownOutcome("connection reset while creating (fake, nothing created)")
        self._seq += 1
        pid = str(100000 + self._seq)
        pi = ProviderInstance(
            provider_id=pid, name=spec.name, status="new", public_ipv4=None, tags=list(spec.tags),
            created_at=datetime.now(UTC), size_slug=spec.offer.size_slug, region=spec.offer.region,
        )
        m = FakeMachine(pi=pi, spec=spec)
        self.machines[pid] = m
        if outcome != "boot_hang":
            m.boot_task = asyncio.create_task(self._boot(m, outcome), name=f"fake-boot-{pid}")
        if outcome == "unknown_created":
            raise UnknownOutcome("timeout while creating (fake, machine was created)")
        return ProviderInstance(**{**pi.__dict__, "tags": list(pi.tags)})

    async def _boot(self, m: FakeMachine, outcome: str | None) -> None:
        await asyncio.sleep(self.boot_seconds / 2)
        m.pi.public_ipv4 = "127.0.0.1"
        await asyncio.sleep(self.boot_seconds / 2)
        assert self.scratch_root is not None
        m.root = self.scratch_root / m.pi.provider_id
        boot = m.spec.bootstrap.model_copy(
            update={"models_dir": str(m.root / "models"), "job_dir": str(m.root / "jobs"), "engine": "mock"}
        )
        m.engine = MockEngine(self.job_seconds)
        if outcome == "engine_oom":
            m.engine.fail_next = "oom"
        elif outcome == "engine_fail":
            m.engine.fail_next = "engine"
        state = build_state(boot, m.engine, simulate_files=True)
        m.server = WorkerServer(state, host="127.0.0.1", port=0)
        await m.server.start()
        m.pi.port = m.server.port
        m.pi.status = "active"

    async def get_instance(self, provider_id: str) -> ProviderInstance | None:
        m = self.machines.get(provider_id)
        if m is None:
            return None
        return ProviderInstance(**{**m.pi.__dict__, "tags": list(m.pi.tags)})

    async def list_instances(self, tag: str) -> list[ProviderInstance]:
        return [ProviderInstance(**{**m.pi.__dict__, "tags": list(m.pi.tags)})
                for m in self.machines.values() if tag in m.pi.tags]

    async def destroy_instance(self, provider_id: str) -> None:
        m = self.machines.pop(provider_id, None)
        if m is None:
            return
        if m.boot_task and not m.boot_task.done():
            m.boot_task.cancel()
            with contextlib.suppress(BaseException):
                await m.boot_task
        if m.server:
            await m.server.stop()
        if m.root:
            shutil.rmtree(m.root, ignore_errors=True)

    async def ensure_network_access(self, tag: str, allowed_cidrs: list[str], port: int) -> None:
        self.firewall_cidrs = list(allowed_cidrs)

    async def account_month_to_date_usd(self) -> float | None:
        return None

    async def gpu_quota(self) -> int | None:
        return None

    async def aclose(self) -> None:
        for pid in list(self.machines):
            await self.destroy_instance(pid)

    # -- test helpers ---------------------------------------------------------------

    def inject_orphan(self, tags: list[str], age_s: float = 3600) -> str:
        from datetime import timedelta

        self._seq += 1
        pid = str(100000 + self._seq)
        pi = ProviderInstance(
            provider_id=pid, name=f"orphan-{pid}", status="active", public_ipv4="127.0.0.9", tags=tags,
            created_at=datetime.now(UTC) - timedelta(seconds=age_s), size_slug="gpu-4000adax1-20gb", region="tor1",
        )
        self.machines[pid] = FakeMachine(pi=pi, spec=None)  # type: ignore[arg-type]
        return pid

    def vanish(self, provider_id: str) -> None:
        """Simulate a machine disappearing outside our control."""
        m = self.machines.pop(provider_id, None)
        if m and m.server:
            asyncio.create_task(m.server.stop())
