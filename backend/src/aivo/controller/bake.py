"""Prepared machine image ("golden snapshot") builder.

Creates the cheapest available GPU machine with the smallest disk (a snapshot
can only be used on sizes whose disk is at least as large), installs the
container runtime, pre-pulls the worker image, cleans machine identity, powers
off, snapshots, transfers the snapshot to the GPU regions in the cache policy,
and destroys the bake machine. Persisted step by step in ``background_runs`` so
it resumes after a restart. Contains no model weights and no user content.

While the machine works, the start-up status server (templates/boot_status.py) reports its step
and log over pinned TLS, so a failure ends the bake at once with the machine's own reason.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from aivo.context import AppContext
from aivo.controller.common import get_offers, utcnow
from aivo.db.models import BackgroundRun, CacheResource
from aivo.events.bus import emit
from aivo.providers.base import deployment_tag
from aivo.worker_client.client import WorkerClient
from aivo.worker_client.tls import make_worker_credentials
from aivo_protocol.worker_api import BootStatus, WorkerBootstrap

log = logging.getLogger(__name__)

WATCH_TIMEOUT_S = 10  # per status probe of the bake machine
LOG_TAIL_LINES = 40


def bake_user_data(boot: WorkerBootstrap) -> str:
    from aivo.providers.digitalocean.cloudinit import render_bake_user_data

    return render_bake_user_data(boot)


class BakeFailed(RuntimeError):
    """The bake machine reported a failure."""


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
            run = (await s.execute(select(BackgroundRun).where(BackgroundRun.id == run_id))).scalar_one()
            run.state, run.finished_at, run.step = state, utcnow(), state
            if error:
                run.data = {**run.data, "error": error[:1000]}
            await emit(s, scope="system", type=f"bake.{state}", ref_id=run_id, payload={"error": error})

    async def run(self, run_id: uuid.UUID) -> None:
        try:
            await self._run(run_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if isinstance(e, BakeFailed):
                log.warning("bake %s failed: %s", run_id, e)
            else:
                log.exception("bake %s failed", run_id)
            await self._cleanup(run_id)
            await self._finish(run_id, "failed", str(e))

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
            await self._ensure_firewall(rs)
            # the machine's own certificate and token: its status server answers only us
            creds = make_worker_credentials(run_id)
            token = secrets.token_urlsafe(32)
            boot = WorkerBootstrap(
                instance_id=str(run_id),
                deployment_id=str(self.app.deployment_id),
                token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                tls_cert_pem=creds.cert_pem,
                tls_key_pem=creds.key_pem,
                listen_port=self.app.env.worker_port,
                worker_image=image,
            )
            watch = {
                "ca_pem": creds.ca_pem,
                "sni": creds.sni_name,
                "token_enc": base64.b64encode(self.app.keyring.seal(token, run_id.bytes)).decode(),
            }
            last_err = None
            for offer in offers[:6]:
                try:
                    pi = await p.create_bake_instance(
                        offer, bake_user_data(boot), ["aivo", deployment_tag(self.app.deployment_id), "aivo-bake"]
                    )
                    data = {
                        "droplet_id": pi.provider_id,
                        "region": offer.region,
                        "disk_gb": offer.disk_gb,
                        "offer": offer.key,
                        "started": utcnow().isoformat(),
                        **watch,
                    }
                    log.info("bake %s: preparing the image on %s in %s", run_id, offer.size_slug, offer.region)
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
                    if pi.public_ipv4:
                        await self._watch(run_id, data, pi.public_ipv4, pi.port or self.app.env.worker_port)
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

    async def _watch(self, run_id: uuid.UUID, data: dict[str, Any], ip: str, port: int) -> None:
        """Read the bake machine's step; raise with its reason (and keep its last log lines) if it failed."""
        if "token_enc" not in data:  # a bake started by an older version
            return
        token = self.app.keyring.open(base64.b64decode(data["token_enc"]), run_id.bytes)
        if token is None:
            return
        try:
            async with WorkerClient(ip, port, data["ca_pem"], data["sni"], token, timeout=WATCH_TIMEOUT_S) as c:
                st = await c.probe()
                if not isinstance(st, BootStatus):
                    return
                if st.stage != data.get("stage"):
                    from aivo.controller.provisioner import stage_label

                    data["stage"] = st.stage
                    log.info("bake %s: %s", run_id, stage_label(st.stage))
                    await self._note(run_id, stage=st.stage)
                if st.failed:
                    tail = [ln.text for ln in (await c.logs(tail=LOG_TAIL_LINES)).lines]
                    for line in tail[-15:]:
                        log.warning("bake %s machine log: %s", run_id, line)
                    await self._note(run_id, log_tail=tail)
                    raise BakeFailed(st.error or "the machine reported a problem")
        except BakeFailed:
            raise
        except Exception as e:  # not answering (yet), or already powering off
            log.debug("bake %s: status not available: %s", run_id, e)

    async def _note(self, run_id: uuid.UUID, **data: Any) -> None:
        async with self.app.sessionmaker() as s, s.begin():
            run = (await s.execute(select(BackgroundRun).where(BackgroundRun.id == run_id))).scalar_one()
            run.data = {**run.data, **data}
            await emit(s, scope="system", type="bake.progress", ref_id=run_id, payload={"stage": data.get("stage")})

    async def _ensure_firewall(self, rs: Any) -> None:
        """The bake machine carries the deployment tag: make sure the deployment's firewall exists."""
        try:
            from aivo.controller.provisioner import Provisioner

            await Provisioner(self.app)._ensure_access(rs)
        except Exception as e:  # never block the bake on the firewall; TLS + token still protect it
            log.warning("could not update firewall before baking: %s", e)

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
