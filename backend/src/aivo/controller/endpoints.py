"""One RunPod queue-based endpoint per GPU type: created on first use, kept in step with the settings.

An endpoint costs nothing while no worker runs, and RunPod keeps a worker prepared on a host for it, so
endpoints are kept rather than deleted. RunPod's queue starts workers when jobs wait (up to "workers per GPU
type"), gives each one job at a time, and stops a worker after it waited ``keep_warm_s`` for a next job.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from aivo.config import RuntimeSettings
from aivo.context import AppContext
from aivo.controller.common import get_offers, offer_for, utcnow
from aivo.db.models import GpuEndpoint, Job
from aivo.providers.base import ConfigError, GpuOffer
from aivo.providers.runpod.catalog import gpu_slug

log = logging.getLogger(__name__)

ACTIVE = ("assigned", "preparing", "running", "fetching")


class EndpointManager:
    def __init__(self, app: AppContext):
        self.app = app
        self._locks: dict[str, asyncio.Lock] = {}

    def name_for(self, gpu_type: str) -> str:
        return f"aivo-{str(self.app.deployment_id)[:8]}-{gpu_slug(gpu_type)}"

    def desired(self, gpu_type: str, rs: RuntimeSettings, *, warm: bool) -> dict[str, Any]:
        env = self.app.env
        gpus = self.app.gpus
        body: dict[str, Any] = {
            "name": self.name_for(gpu_type),
            "type": "QUEUE",
            "image": env.worker_image,
            "gpu": gpus.gpu_selection(gpu_type),
            "workers": {
                "min": 1 if warm else 0,
                "max": max(1, rs.max_workers_per_gpu),
                "idleTimeout": max(5, min(3600, rs.keep_warm_s)),
                "initTimeout": 1800,
            },
            # add a worker as soon as a job waits a second: each waiting job gets its own worker (up to max)
            "scaling": {"type": "QUEUE_DELAY", "queueDelay": 1},
            "disk": env.runpod_disk_gb,
            "env": {
                "AIVO_DEPLOYMENT_ID": str(self.app.deployment_id),
                "AIVO_GPU_TYPE": gpu_type,
            },
            # the longest a job may run on a worker: the creation itself plus downloading the model
            "timeout": (rs.job_timeout_s + 1200) * 1000,
            "flashboot": "FLASHBOOT",
        }
        dcs = gpus.data_centers(gpu_type)
        if dcs:
            body["dataCenterIds"] = dcs
        if env.runpod_global_volume_id:
            # any data center; sending both lists switches an endpoint that had a network volume
            body["mounts"] = {"network": [], "global": [{"volumeId": env.runpod_global_volume_id}]}
            body["env"]["AIVO_VOLUME_KIND"] = "global"  # the worker keeps working copies on its own disk
        elif env.runpod_network_volume_id:
            body["mounts"] = {"global": [], "network": [{"volumeId": env.runpod_network_volume_id}]}
        return body

    @staticmethod
    def config_hash(body: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    async def ensure(self, offer: GpuOffer) -> GpuEndpoint:
        """The endpoint for this GPU type, created or updated if needed. Raises ProviderError."""
        gpu_type = offer.size_slug
        lock = self._locks.setdefault(gpu_type, asyncio.Lock())
        async with lock:
            app = self.app
            rs = await app.runtime()
            if gpu_type not in app.gpus.types:
                await get_offers(app, force=True)
            async with app.sessionmaker() as s, s.begin():
                row = (
                    await s.execute(select(GpuEndpoint).where(GpuEndpoint.gpu_type == gpu_type).with_for_update())
                ).scalar_one_or_none()
                if row is None:
                    row = GpuEndpoint(gpu_type=gpu_type, gpu_name=offer.gpu_model[:64], status="creating")
                    s.add(row)
                row.gpu_class = offer.gpu_class
                row.vram_gb = offer.vram_gb
                row.price_hourly = offer.price_hourly
                warm = row.keep_warm_until is not None and row.keep_warm_until > utcnow()
                runpod_id, old_hash = row.runpod_id, row.config_hash
            body = self.desired(gpu_type, rs, warm=warm)
            h = self.config_hash(body)
            error: str | None = None
            try:
                if runpod_id and old_hash != h:
                    if await app.runpod.get_endpoint(runpod_id) is None:
                        runpod_id = None
                    else:
                        await app.runpod.update_endpoint(runpod_id, {k: v for k, v in body.items() if k != "type"})
                if not runpod_id:
                    created = await app.runpod.create_endpoint(body)
                    runpod_id = str(created.get("id") or "")
                    if not runpod_id:
                        raise ConfigError(f"RunPod did not return an endpoint id: {created}")
                    log.info("created RunPod endpoint %s for %s", runpod_id, gpu_type)
            except Exception as e:
                error = str(e)[:1000]
                raise
            finally:
                async with app.sessionmaker() as s, s.begin():
                    row = (await s.execute(select(GpuEndpoint).where(GpuEndpoint.gpu_type == gpu_type))).scalar_one()
                    if error is None:
                        row.runpod_id, row.config_hash, row.status, row.error = runpod_id, h, "ready", None
                        row.last_used_at = utcnow()
                    else:
                        row.status, row.error = ("error" if not row.runpod_id else row.status), error
            async with app.sessionmaker() as s:
                return (await s.execute(select(GpuEndpoint).where(GpuEndpoint.gpu_type == gpu_type))).scalar_one()

    async def set_keep_warm(self, gpu_type: str, minutes: int) -> GpuEndpoint:
        """Keep one worker of this GPU type running (billed continuously) for a while, or stop doing so."""
        async with self.app.sessionmaker() as s, s.begin():
            row = (await s.execute(select(GpuEndpoint).where(GpuEndpoint.gpu_type == gpu_type))).scalar_one()
            row.keep_warm_until = utcnow() + timedelta(minutes=minutes) if minutes > 0 else None
        offers = await get_offers(self.app)
        offer = offer_for(offers, gpu_type)
        if offer is None:
            raise ConfigError(f"{gpu_type} is not offered right now")
        return await self.ensure(offer)

    async def sync(self) -> None:
        """Keep endpoints in step: settings or a new release changed (new config), keep-warm ended, or RunPod
        lowered max workers of an endpoint unused for days. Deletes this deployment's endpoints aivo no longer
        knows."""
        app = self.app
        async with app.sessionmaker() as s:
            rows = (await s.execute(select(GpuEndpoint))).scalars().all()
            busy = set(
                (await s.execute(select(Job.endpoint_id).where(Job.state.in_(ACTIVE), Job.endpoint_id.is_not(None))))
                .scalars()
                .all()
            )
        rs = await app.runtime()
        try:
            offers = await get_offers(app)
        except Exception as e:
            log.info("endpoint sync skipped: %s", e)
            return
        for row in rows:
            if not row.runpod_id:
                continue
            offer = offer_for(offers, row.gpu_type)
            if offer is None:
                continue
            warm = row.keep_warm_until is not None and row.keep_warm_until > utcnow()
            try:
                desired_hash = self.config_hash(self.desired(row.gpu_type, rs, warm=warm))
                if desired_hash != row.config_hash and row.id not in busy:
                    await self.ensure(offer)
                    continue
                live = await app.runpod.get_endpoint(row.runpod_id)
                if live is None:  # deleted in the RunPod console: made again on next use
                    async with app.sessionmaker() as s, s.begin():
                        r = (await s.execute(select(GpuEndpoint).where(GpuEndpoint.id == row.id))).scalar_one()
                        r.runpod_id, r.config_hash, r.status = None, None, "creating"
                    continue
                if int((live.get("workers") or {}).get("max", 0)) < max(1, rs.max_workers_per_gpu):
                    await app.runpod.update_endpoint(
                        row.runpod_id, {"workers": {"max": max(1, rs.max_workers_per_gpu)}}
                    )
            except Exception as e:
                log.warning("could not sync endpoint %s: %s", row.gpu_type, e)
        await self._delete_orphans({r.runpod_id for r in rows if r.runpod_id})

    async def _delete_orphans(self, known: set[str]) -> None:
        dep = str(self.app.deployment_id)
        try:
            endpoints = await self.app.runpod.list_endpoints()
        except Exception as e:
            log.info("could not list RunPod endpoints: %s", e)
            return
        for ep in endpoints:
            env = ep.get("env") or {}
            if env.get("AIVO_DEPLOYMENT_ID") != dep or str(ep.get("id")) in known:
                continue
            log.warning("deleting RunPod endpoint %s (%s): aivo no longer uses it", ep.get("id"), ep.get("name"))
            try:
                await self.app.runpod.delete_endpoint(str(ep["id"]))
            except Exception as e:
                log.warning("could not delete endpoint %s: %s", ep.get("id"), e)
