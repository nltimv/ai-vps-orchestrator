"""Fake cloud provider for local development and tests.

Each "machine" is an in-process worker agent (mock engine) served over real TLS
on a random localhost port, so the backend exercises the exact same code paths
(bootstrap, pinned TLS, token, jobs, downloads) as with a real GPU droplet,
without spending money.

Before the agent starts, each machine runs the real start-up status server
(``templates/boot_status.py``) and walks through simulated start-up steps with a
setup log, like ``bootstrap.sh`` on a real machine.

Failure injection (``chaos``): queue outcomes per ``"<size>:<region>"`` (or ``"*"``):
``capacity``, ``quota``, ``volume_busy``, ``unknown_created``, ``unknown_lost``,
``boot_hang`` (never gets an address), ``boot_silent`` (address, but nothing answers),
``boot_reports_failure`` (the start-up script reports a missing GPU driver),
``boot_slow_progress`` (each start-up step takes ``slow_step_s``), ``engine_start_fail``
(the AI engine can't start), ``engine_oom``, ``engine_fail``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import shutil
import socket
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

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
BOOT_STATUS_PY = Path(__file__).parents[1] / "digitalocean" / "templates" / "boot_status.py"
NO_DRIVER = (
    "This machine has an NVIDIA GPU but no GPU driver. It was started from an operating-system image "
    "without GPU drivers: set AIVO_DO_BASE_IMAGE to a GPU image (default gpu-h100x1-base)."
)


@cache
def boot_status_module() -> ModuleType:
    """The exact status server that real machines run, loaded from its template."""
    spec = importlib.util.spec_from_file_location("aivo_boot_status", BOOT_STATUS_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _FailingEngine(MockEngine):
    async def start(self) -> None:
        self._say("Traceback (most recent call last):", "error")
        self._say("RuntimeError: CUDA driver initialization failed (simulated)", "error")
        raise RuntimeError("CUDA driver initialization failed (simulated)")


@dataclass
class FakeMachine:
    pi: ProviderInstance
    spec: InstanceSpec
    server: WorkerServer | None = None
    boot_task: asyncio.Task[None] | None = None
    engine: MockEngine | None = None
    root: Path | None = None
    status_server: Any = None  # boot_status.BootStatusServer while "starting up"
    status_thread: threading.Thread | None = None


@dataclass
class FakeProvider:
    boot_seconds: float = 1.0
    job_seconds: float = 1.0
    slow_step_s: float = 2.0  # per start-up step with the boot_slow_progress chaos
    bake_seconds: float | None = None  # how long preparing a machine image takes (default: boot_seconds)
    scratch_root: Path | None = None
    name: str = "fake"
    chaos: dict[str, list[str]] = field(default_factory=dict)
    sold_out: set[str] = field(default_factory=set)  # "<size>:<region>" hidden from offers
    machines: dict[str, FakeMachine] = field(default_factory=dict)
    firewall_cidrs: list[str] = field(default_factory=list)
    create_calls: int = 0
    snapshots: dict[str, dict] = field(default_factory=dict)  # type: ignore[type-arg]
    transfers: list[tuple[str, str]] = field(default_factory=list)
    deleted_images: list[str] = field(default_factory=list)
    _seq: int = 0
    _bg: set[asyncio.Task[None]] = field(default_factory=set)

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
                        provider=self.name,
                        size_slug=s["slug"],
                        region=region,
                        gpu_model=s["label"],
                        gpu_class=s["model"],
                        gpu_count=s["count"],
                        vram_gb=s["vram"],
                        vcpus=s["vcpus"],
                        ram_gb=s["ram"],
                        disk_gb=s["disk"],
                        scratch_gb=s["scratch"],
                        price_hourly=s["price"],
                        vendor=s["vendor"],
                    )
                )
        return out

    def regions(self) -> dict[str, RegionMeta]:
        return DO_REGIONS

    async def boot_image(self, offer: GpuOffer, strategy: str) -> BootImage:
        return BootImage(
            ref="fake-snapshot" if strategy != "base" else "fake-base",
            kind="snapshot" if strategy != "base" else "base",
        )

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
            provider_id=pid,
            name=spec.name,
            status="new",
            public_ipv4=None,
            tags=list(spec.tags),
            created_at=datetime.now(UTC),
            size_slug=spec.offer.size_slug,
            region=spec.offer.region,
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
        assert self.scratch_root is not None
        m.root = self.scratch_root / m.pi.provider_id
        m.root.mkdir(parents=True, exist_ok=True)
        if outcome == "boot_silent":  # an address, but nothing ever answers
            m.pi.port = _free_port()
            m.pi.status = "active"
            return
        setup_log = m.root / "setup.log"
        status_file = m.root / "boot-status.json"
        bs = boot_status_module()

        def say(text: str) -> None:
            with setup_log.open("a") as fh:
                fh.write(f"[aivo {time.strftime('%H:%M:%S')}] {text}\n")

        def stage(name: str, detail: str | None = None) -> None:
            say(f"== step: {name}" + (f" ({detail})" if detail else ""))
            bs.write_status(str(status_file), stage=name, detail=detail)

        say("bootstrap start (fake machine)")
        bs.write_status(str(status_file), stage="starting", failed=False, error=None)
        srv = bs.make_server(
            m.spec.bootstrap.model_dump(),
            host="127.0.0.1",
            port=0,
            status_path=str(status_file),
            log_path=str(setup_log),
        )
        m.status_server = srv
        m.status_thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        m.status_thread.start()
        m.pi.port = srv.server_address[1]
        m.pi.status = "active"

        step = self.slow_step_s if outcome == "boot_slow_progress" else self.boot_seconds / 10
        image = m.spec.bootstrap.worker_image or "aivo-worker"
        stage("preparing_disks")
        say("model storage: 100G free")
        await asyncio.sleep(step)
        stage("checking_gpu")
        if outcome == "boot_reports_failure":
            say(f"FAILED: {NO_DRIVER}")
            bs.write_status(str(status_file), failed=True, error=NO_DRIVER)
            return  # the status server keeps reporting the failure
        say("GPU 0: Mock GPU (fake machine)")
        await asyncio.sleep(step)
        stage("installing_runtime")
        say("Docker is already installed")
        await asyncio.sleep(step)
        stage("downloading_software", image)
        say(f"{image.rsplit(':', 1)[-1]}: Pulling from {image.rsplit(':', 1)[0]}")
        await asyncio.sleep(step)
        say("Status: Image is up to date")
        stage("starting_engine")
        await self._stop_status_server(m)
        say("bootstrap done; the worker agent takes over")

        boot = m.spec.bootstrap.model_copy(
            update={"models_dir": str(m.root / "models"), "job_dir": str(m.root / "jobs"), "engine": "mock"}
        )
        m.engine = _FailingEngine(self.job_seconds) if outcome == "engine_start_fail" else MockEngine(self.job_seconds)
        if outcome == "engine_oom":
            m.engine.fail_next = "oom"
        elif outcome == "engine_fail":
            m.engine.fail_next = "engine"
        state = build_state(boot, m.engine, simulate_files=True, setup_log=setup_log)
        m.server = WorkerServer(state, host="127.0.0.1", port=m.pi.port)  # same port, like the real handover
        await m.server.start()

    async def _stop_status_server(self, m: FakeMachine) -> None:
        srv, m.status_server = m.status_server, None
        if srv is not None:
            await asyncio.to_thread(srv.shutdown)
            srv.server_close()

    async def get_instance(self, provider_id: str) -> ProviderInstance | None:
        m = self.machines.get(provider_id)
        if m is None:
            return None
        return ProviderInstance(**{**m.pi.__dict__, "tags": list(m.pi.tags)})

    async def list_instances(self, tag: str) -> list[ProviderInstance]:
        return [
            ProviderInstance(**{**m.pi.__dict__, "tags": list(m.pi.tags)})
            for m in self.machines.values()
            if tag in m.pi.tags
        ]

    async def destroy_instance(self, provider_id: str) -> None:
        m = self.machines.pop(provider_id, None)
        if m is None:
            return
        if m.boot_task and not m.boot_task.done():
            m.boot_task.cancel()
            with contextlib.suppress(BaseException):
                await m.boot_task
        await self._stop_status_server(m)
        if m.server:
            await m.server.stop()
        if m.root:
            shutil.rmtree(m.root, ignore_errors=True)

    async def ensure_network_access(
        self, tag: str, allowed_cidrs: list[str], port: int, *, ssh_cidrs: list[str] | None = None
    ) -> None:
        self.firewall_cidrs = list(allowed_cidrs)
        self.ssh_cidrs = list(ssh_cidrs or [])

    async def account_month_to_date_usd(self) -> float | None:
        return None

    async def gpu_quota(self) -> int | None:
        return None

    async def aclose(self) -> None:
        for pid in list(self.machines):
            await self.destroy_instance(pid)

    # -- prepared-image primitives (bake) ----------------------------------------------

    async def create_bake_instance(self, offer: GpuOffer, user_data: str, tags: list[str]) -> ProviderInstance:
        self._seq += 1
        pid = str(100000 + self._seq)
        pi = ProviderInstance(
            provider_id=pid,
            name=f"aivo-bake-{pid}",
            status="active",
            public_ipv4="127.0.0.2",
            tags=tags,
            created_at=datetime.now(UTC),
            size_slug=offer.size_slug,
            region=offer.region,
        )
        self.machines[pid] = FakeMachine(pi=pi, spec=None)  # type: ignore[arg-type]

        async def _finish() -> None:
            await asyncio.sleep(self.bake_seconds if self.bake_seconds is not None else self.boot_seconds)
            pi.status = "off"

        self._bg.add(t := asyncio.create_task(_finish()))
        t.add_done_callback(self._bg.discard)
        return pi

    async def snapshot(self, provider_id: str, name: str) -> int:
        self.snapshots[name] = {
            "id": 900000 + len(self.snapshots),
            "name": name,
            "size_gigabytes": 22.5,
            "min_disk_size": 500,
            "regions": [],
        }
        return 1

    async def wait_action(self, action_id: int, timeout_s: float = 3600, poll_s: float = 10) -> None:
        await asyncio.sleep(0.01)

    async def find_snapshot(self, name: str) -> dict | None:  # type: ignore[type-arg]
        return self.snapshots.get(name)

    async def transfer_image(self, image_id: str, region: str) -> int:
        self.transfers.append((image_id, region))
        return 2

    async def delete_image(self, image_id: str) -> None:
        self.deleted_images.append(image_id)

    # -- test helpers ---------------------------------------------------------------

    def inject_orphan(self, tags: list[str], age_s: float = 3600) -> str:
        from datetime import timedelta

        self._seq += 1
        pid = str(100000 + self._seq)
        pi = ProviderInstance(
            provider_id=pid,
            name=f"orphan-{pid}",
            status="active",
            public_ipv4="127.0.0.9",
            tags=tags,
            created_at=datetime.now(UTC) - timedelta(seconds=age_s),
            size_slug="gpu-4000adax1-20gb",
            region="tor1",
        )
        self.machines[pid] = FakeMachine(pi=pi, spec=None)  # type: ignore[arg-type]
        return pid

    def vanish(self, provider_id: str) -> None:
        """Simulate a machine disappearing outside our control."""
        m = self.machines.pop(provider_id, None)
        if m and m.server:
            self._bg.add(t := asyncio.create_task(m.server.stop()))
            t.add_done_callback(self._bg.discard)
