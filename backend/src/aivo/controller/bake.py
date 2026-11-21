"""Prepared machine image ("golden snapshot") builder.

Creates the cheapest available GPU machine with the smallest disk (a snapshot
can only be used on sizes whose disk is at least as large), installs the
container runtime, pre-pulls the worker image, cleans machine identity, powers
off, snapshots, transfers the snapshot to the GPU regions in the cache policy,
and destroys the bake machine. Persisted step by step in ``background_runs`` so
it resumes after a restart. Contains no model weights and no user content.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from aivo.context import AppContext
from aivo.controller.common import get_offers, utcnow
from aivo.db.models import BackgroundRun, CacheResource
from aivo.events.bus import emit
from aivo.providers.base import deployment_tag

log = logging.getLogger(__name__)

BAKE_SCRIPT = r"""#!/bin/bash
set -euxo pipefail
exec >>/var/log/aivo-bake.log 2>&1
IMAGE="__IMAGE__"
# a GPU machine without a driver would produce a prepared image that can never run the AI engine
if ! command -v nvidia-smi >/dev/null 2>&1 && grep -qsx 0x10de /sys/bus/pci/devices/*/vendor; then
  echo "[aivo] FAILED: NVIDIA GPU without a driver: set AIVO_DO_BASE_IMAGE to a GPU image"; exit 1
fi
if ! command -v docker >/dev/null 2>&1; then curl -fsSL https://get.docker.com | sh; fi
if ! command -v nvidia-ctk >/dev/null 2>&1 && command -v nvidia-smi >/dev/null 2>&1; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
fi
systemctl enable docker
systemctl restart docker
docker pull "$IMAGE"
# self-test on GPU bakes
if command -v nvidia-smi >/dev/null 2>&1; then docker run --rm --gpus all "$IMAGE" python -c "import torch; assert torch.cuda.is_available()"; fi
apt-get clean; rm -rf /var/lib/apt/lists/* /var/log/*.gz /tmp/*
cloud-init clean --logs --machine-id
rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys
sync
poweroff
"""


def bake_user_data(image: str) -> str:
    import base64

    script = BAKE_SCRIPT.replace("__IMAGE__", image)
    return (
        "#cloud-config\nwrite_files:\n- path: /opt/aivo/bake.sh\n  permissions: '0755'\n  encoding: b64\n"
        f"  content: {base64.b64encode(script.encode()).decode()}\nruncmd:\n- [bash, /opt/aivo/bake.sh]\n"
    )


class Baker:
    def __init__(self, app: AppContext, *, poll_s: float = 15.0):
        self.app = app
        self.poll_s = poll_s

    def supported(self) -> bool:
        p = self.app.provider
        return all(
            hasattr(p, m)
            for m in (
                "create_bake_instance",
                "snapshot",
                "wait_action",
                "find_snapshot",
                "transfer_image",
                "delete_image",
            )
        )

    async def start(self, vendor: str = "nvidia") -> uuid.UUID:
        async with self.app.sessionmaker() as s, s.begin():
            run = BackgroundRun(
                kind="bake",
                state="running",
                step="create",
                params={"vendor": vendor, "image": self.app.env.worker_image},
            )
            s.add(run)
            await s.flush()
            await emit(s, scope="system", type="bake.started", ref_id=run.id)
            return run.id

    async def _save(self, run_id: uuid.UUID, step: str, **data: Any) -> None:
        async with self.app.sessionmaker() as s, s.begin():
            run = (await s.execute(select(BackgroundRun).where(BackgroundRun.id == run_id))).scalar_one()
            run.step = step
            run.data = {**run.data, **data}
            run.log = [*run.log, {"at": datetime.now(UTC).isoformat(), "step": step}]

    async def _finish(self, run_id: uuid.UUID, state: str, error: str | None = None) -> None:
        async with self.app.sessionmaker() as s, s.begin():
            await s.execute(
                update(BackgroundRun)
                .where(BackgroundRun.id == run_id)
                .values(state=state, finished_at=utcnow(), step=error or state)
            )
            await emit(s, scope="system", type=f"bake.{state}", ref_id=run_id, payload={"error": error})

    async def run(self, run_id: uuid.UUID) -> None:
        try:
            await self._run(run_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("bake %s failed", run_id)
            await self._cleanup(run_id)
            await self._finish(run_id, "failed", str(e)[:300])

    async def _run(self, run_id: uuid.UUID) -> None:
        if not self.supported():
            await self._finish(run_id, "failed", "this provider can't prepare machine images")
            return
        p: Any = self.app.provider
        async with self.app.sessionmaker() as s:
            run = (await s.execute(select(BackgroundRun).where(BackgroundRun.id == run_id))).scalar_one()
        data, params = dict(run.data), dict(run.params)
        rs = await self.app.runtime(fresh=True)
        image = params["image"]
        if run.step == "create":
            offers = [
                o
                for o in await get_offers(self.app, force=True)
                if o.vendor == params["vendor"] and o.gpu_count == 1 and o.available
            ]
            if not offers:
                raise RuntimeError("no GPU machine available to prepare the image on")
            # smallest disk first (snapshot min_disk_size), then cheapest
            offers.sort(key=lambda o: (o.disk_gb, o.price_hourly))
            last_err = None
            for offer in offers[:6]:
                try:
                    pi = await p.create_bake_instance(
                        offer, bake_user_data(image), ["aivo", deployment_tag(self.app.deployment_id), "aivo-bake"]
                    )
                    data = {
                        "droplet_id": pi.provider_id,
                        "region": offer.region,
                        "disk_gb": offer.disk_gb,
                        "offer": offer.key,
                        "started": utcnow().isoformat(),
                    }
                    break
                except Exception as e:
                    last_err = e
            else:
                raise RuntimeError(f"could not start a machine to prepare the image: {last_err}")
            await self._save(run_id, "wait_off", **data)
            run.step = "wait_off"
        if run.step == "wait_off":
            missing = 0
            for _ in range(int(3600 / self.poll_s)):
                pi = await p.get_instance(data["droplet_id"])
                if pi is None:  # tolerate a brief gap in the provider's listing
                    missing += 1
                    if missing >= 3:
                        raise RuntimeError(
                            f"the machine preparing the image (#{data['droplet_id']}) was deleted before it finished"
                        )
                elif pi.status == "off":
                    break
                else:
                    missing = 0
                await asyncio.sleep(self.poll_s)
            else:
                raise TimeoutError("bake machine did not finish in an hour")
            name = f"aivo-{params['vendor']}-{utcnow():%Y%m%d%H%M}"
            action = await p.snapshot(data["droplet_id"], name)
            await self._save(run_id, "snapshotting", snapshot_name=name, action=action)
            run.step, data["snapshot_name"], data["action"] = "snapshotting", name, action
        if run.step == "snapshotting":
            await p.wait_action(int(data["action"]), poll_s=self.poll_s)
            snap = await p.find_snapshot(data["snapshot_name"])
            if not snap:
                raise RuntimeError("snapshot not found after creation")
            await self._save(
                run_id,
                "transfer",
                image_id=str(snap["id"]),
                size_gb=snap.get("size_gigabytes"),
                min_disk_gb=snap.get("min_disk_size"),
            )
            data.update(
                image_id=str(snap["id"]), size_gb=snap.get("size_gigabytes"), min_disk_gb=snap.get("min_disk_size")
            )
            await p.destroy_instance(data["droplet_id"])
            run.step = "transfer"
        if run.step == "transfer":
            regions = [data["region"], *[r for r in rs.cache_policy.snapshot_regions if r != data["region"]]]
            for region in regions[1:]:
                act = await p.transfer_image(data["image_id"], region)
                await p.wait_action(act, poll_s=self.poll_s)
            async with self.app.sessionmaker() as s, s.begin():
                old = (
                    (
                        await s.execute(
                            select(CacheResource).where(
                                CacheResource.kind == "snapshot",
                                CacheResource.vendor == params["vendor"],
                                CacheResource.status.in_(("ready", "verified")),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for o in old:
                    o.status = "deprecated"
                size = float(data.get("size_gb") or 25)
                for region in regions:
                    s.add(
                        CacheResource(
                            provider=self.app.provider.name,
                            kind="snapshot",
                            region=region,
                            provider_resource_id=data["image_id"],
                            name=data["snapshot_name"],
                            vendor=params["vendor"],
                            worker_image=image,
                            size_gb=size,
                            min_disk_gb=data.get("min_disk_gb"),
                            status="ready",
                            monthly_cost_est=round(size * 0.06, 2),
                        )
                    )
            await self._save(run_id, "gc")
            run.step = "gc"
        if run.step == "gc":
            await self.gc_snapshots()
        await self._finish(run_id, "succeeded")

    async def _cleanup(self, run_id: uuid.UUID) -> None:
        async with self.app.sessionmaker() as s:
            run = (await s.execute(select(BackgroundRun).where(BackgroundRun.id == run_id))).scalar_one()
        did = run.data.get("droplet_id")
        if did:
            try:
                await self.app.provider.destroy_instance(did)
            except Exception as e:
                log.warning("could not destroy bake machine %s: %s", did, e)

    async def gc_snapshots(self) -> int:
        """Delete deprecated prepared images no longer referenced by a ready one."""
        p: Any = self.app.provider
        n = 0
        async with self.app.sessionmaker() as s, s.begin():
            dep = (
                (
                    await s.execute(
                        select(CacheResource).where(
                            CacheResource.kind == "snapshot", CacheResource.status == "deprecated"
                        )
                    )
                )
                .scalars()
                .all()
            )
            live_ids = set(
                (
                    await s.execute(
                        select(CacheResource.provider_resource_id).where(
                            CacheResource.kind == "snapshot", CacheResource.status.in_(("ready", "verified"))
                        )
                    )
                )
                .scalars()
                .all()
            )
            for r in dep:
                if r.provider_resource_id and r.provider_resource_id not in live_ids:
                    try:
                        await p.delete_image(r.provider_resource_id)
                    except Exception as e:
                        log.warning("could not delete old image: %s", e)
                        continue
                r.status = "deleted"
                n += 1
        return n
