"""Bring a machine from ``pending`` to ``ready``.

* Tries the ranked candidate offers in order (capacity errors -> next candidate).
* Never retries a create blindly after an unknown outcome: it first looks the
  machine up by its unique tag and adopts it if it exists.
* Resumable: all progress lives in the ``instances`` row, so after a restart
  the reconciler simply starts a new provisioner for the same row.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from aivo.context import AppContext
from aivo.controller.common import record_attempt, record_phase, utcnow, worker_client
from aivo.controller.lifecycle import transition_instance, transition_job
from aivo.db.models import CacheResource, Instance, Job
from aivo.providers.base import (
    AuthError,
    CapacityError,
    ConfigError,
    GpuOffer,
    InstanceSpec,
    ProviderError,
    QuotaError,
    RateLimited,
    UnknownOutcome,
    VolumeBusy,
    instance_tag,
    tags_for,
)
from aivo.worker_client.client import WorkerUnreachable
from aivo.worker_client.tls import make_worker_credentials
from aivo_protocol import protocol_compatible
from aivo_protocol.worker_api import WatchdogConfig, WorkerBootstrap

log = logging.getLogger(__name__)

PAUSE_BETWEEN_ATTEMPTS_S = 2.0


class Provisioner:
    def __init__(self, app: AppContext, *, pause_s: float = PAUSE_BETWEEN_ATTEMPTS_S, poll_s: float = 2.0):
        self.app = app
        self.pause_s = pause_s
        self.poll_s = poll_s

    async def run(self, instance_id: uuid.UUID) -> None:
        try:
            await self._run(instance_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("provisioner for %s crashed", instance_id)
            await self._fail(instance_id, f"internal error: {e}", requeue=True)

    async def _load(self, instance_id: uuid.UUID) -> Instance | None:
        async with self.app.sessionmaker() as s:
            return (await s.execute(select(Instance).where(Instance.id == instance_id))).scalar_one_or_none()

    async def _run(self, instance_id: uuid.UUID) -> None:
        inst = await self._load(instance_id)
        if inst is None:
            return
        if inst.state in ("pending", "creating"):
            ok = await self._create(inst)
            if not ok:
                return
            inst = await self._load(instance_id)
            assert inst is not None
        if inst.state == "provisioning":
            if not await self._wait_ip(inst):
                return
            inst = await self._load(instance_id)
            assert inst is not None
        if inst.state == "booting":
            await self._wait_healthy(inst)

    # ------------------------------------------------------------------ create

    async def _create(self, inst: Instance) -> bool:
        app = self.app
        rs = await app.runtime(fresh=True)
        deadline = time.monotonic() + rs.provision_deadline_s
        candidates: list[dict[str, Any]] = list(inst.candidates or [])

        # resume after a crash mid-create: the request may or may not have gone through
        if inst.state == "creating" and inst.provider_instance_id is None:
            if await self._adopt_by_tag(inst):
                return True
            async with app.sessionmaker() as s, s.begin():
                await transition_instance(s, inst.id, "creating", "pending")

        idx = inst.attempt_index
        attempts = 0
        use_volume = True
        retried_unknown = False
        while idx < len(candidates) and attempts < rs.max_create_attempts and time.monotonic() < deadline:
            cand = candidates[idx]
            offer = GpuOffer(**cand["offer"])
            attempts += 1
            try:
                spec, token = await self._prepare(inst, offer, cand, use_volume=use_volume)
            except ProviderError as e:
                await self._record(inst.id, offer, "error", idx, e)
                idx += 1
                continue
            t0 = time.monotonic()
            try:
                pi = await app.provider.create_instance(spec)
            except CapacityError as e:
                await self._record(inst.id, offer, "capacity", idx, e, t0)
                idx, use_volume, retried_unknown = idx + 1, True, False
                await self._back_to_pending(inst.id, idx)
                await asyncio.sleep(self.pause_s)
                continue
            except VolumeBusy as e:
                await self._record(inst.id, offer, "volume_busy", idx, e, t0)
                use_volume = False
                await self._back_to_pending(inst.id, idx, release_volume=True)
                continue
            except QuotaError as e:
                await self._record(inst.id, offer, "quota", idx, e, t0)
                await self._fail(inst.id, "Your cloud account's GPU limit is reached.", requeue=True,
                                 code="quota")
                return False
            except (AuthError, ConfigError) as e:
                await self._record(inst.id, offer, "error", idx, e, t0)
                await self._fail(inst.id, "The cloud account is not set up correctly (check the API token).",
                                 requeue=False, code="config")
                return False
            except RateLimited as e:
                wait = max(5.0, (e.reset_at or time.time() + 30) - time.time())
                log.warning("rate limited; pausing provisioning for %.0fs", wait)
                await self._back_to_pending(inst.id, idx)
                attempts -= 1
                await asyncio.sleep(min(wait, 120))
                continue
            except UnknownOutcome as e:
                await self._record(inst.id, offer, "error", idx, e, t0)
                if await self._adopt_by_tag(inst):
                    return True
                await self._back_to_pending(inst.id, idx)
                if retried_unknown:
                    idx += 1
                retried_unknown = True
                continue
            except ProviderError as e:
                await self._record(inst.id, offer, "error", idx, e, t0)
                idx += 1
                await self._back_to_pending(inst.id, idx)
                await asyncio.sleep(self.pause_s)
                continue
            # success
            async with app.sessionmaker() as s, s.begin():
                await record_attempt(s, inst.id, offer, "created", rank=idx,
                                     duration_ms=int((time.monotonic() - t0) * 1000))
                await transition_instance(
                    s, inst.id, "creating", "provisioning",
                    provider_instance_id=pi.provider_id, create_accepted_at=utcnow(),
                    public_ipv4=pi.public_ipv4,
                    event_payload={"size": offer.size_slug, "region": offer.region},
                )
            del token
            return True

        reason = "no_capacity"
        await self._fail(inst.id, "No suitable GPU machine is available right now.", requeue=True, code=reason)
        return False

    async def _prepare(
        self, inst: Instance, offer: GpuOffer, cand: dict[str, Any], *, use_volume: bool
    ) -> tuple[InstanceSpec, str]:
        """Mint fresh credentials for this attempt and move the row to ``creating``."""
        app = self.app
        rs = await app.runtime()
        creds = make_worker_credentials(inst.id)
        token = secrets.token_urlsafe(32)
        image = await app.provider.boot_image(offer, rs.boot_strategy)
        volume_ids: list[str] = []
        volume_row_id = None
        async with app.sessionmaker() as s, s.begin():
            if use_volume and rs.cache_policy.volume_enabled:
                vol = (
                    await s.execute(
                        update(CacheResource)
                        .where(
                            CacheResource.kind == "volume", CacheResource.region == offer.region,
                            CacheResource.status.in_(("ready", "verified")),
                            CacheResource.attached_instance_id.is_(None),
                        )
                        .values(attached_instance_id=inst.id, last_used_at=utcnow())
                        .returning(CacheResource.id, CacheResource.provider_resource_id)
                    )
                ).first()
                if vol:
                    volume_row_id, volume_ids = vol[0], [vol[1]]
            watchdog = WatchdogConfig()
            if rs.watchdog_enabled and app.env.do_watchdog_token:
                watchdog = WatchdogConfig(
                    enabled=True, token=app.env.do_watchdog_token.get_secret_value(),
                    max_silence_s=1200, deadline_unix=time.time() + rs.max_lifetime_s + rs.lifetime_grace_s + 600,
                )
            ok = await transition_instance(
                s, inst.id, "pending", "creating",
                offer_key=offer.key, size_slug=offer.size_slug, region=offer.region,
                gpu_class=offer.gpu_class or "unknown", gpu_model=offer.gpu_model, vendor=offer.vendor,
                vram_gb=offer.vram_gb, ram_gb=offer.ram_gb, price_hourly=offer.price_hourly,
                image_ref=image.ref, image_kind=image.kind, worker_image=app.env.worker_image,
                cert_ca_pem=creds.ca_pem, sni_name=creds.sni_name,
                token_enc=app.keyring.seal(token, inst.id.bytes), port=app.env.worker_port,
                volume_resource_id=volume_row_id,
                event_payload={"size": offer.size_slug, "region": offer.region},
            )
            if not ok:
                raise ProviderError("instance is no longer pending")
        boot = WorkerBootstrap(
            instance_id=str(inst.id), deployment_id=str(app.deployment_id),
            token_sha256=hashlib.sha256(token.encode()).hexdigest(),
            tls_cert_pem=creds.cert_pem, tls_key_pem=creds.key_pem, listen_port=app.env.worker_port,
            engine="comfy", volume_mount="/mnt/aivo-cache" if volume_ids else None,
            watchdog=watchdog, worker_image=app.env.worker_image,
        )
        spec = InstanceSpec(
            instance_id=inst.id, name=inst.name, offer=offer, image=image, bootstrap=boot,
            tags=tags_for(app.deployment_id, inst.id), volume_ids=volume_ids, worker_image=app.env.worker_image,
        )
        return spec, token

    async def _adopt_by_tag(self, inst: Instance) -> bool:
        for delay in (5.0, 15.0):
            await asyncio.sleep(delay * (self.poll_s / 2.0))
            found = await self.app.provider.list_instances(instance_tag(inst.id))
            if found:
                pi = found[0]
                async with self.app.sessionmaker() as s, s.begin():
                    cur = (await s.execute(select(Instance).where(Instance.id == inst.id))).scalar_one()
                    offer = GpuOffer(**cur.candidates[cur.attempt_index]["offer"]) if cur.candidates else None
                    if offer:
                        await record_attempt(s, inst.id, offer, "unknown_adopted", rank=cur.attempt_index)
                    await transition_instance(
                        s, inst.id, ("creating", "pending"), "provisioning",
                        provider_instance_id=pi.provider_id, create_accepted_at=utcnow(), public_ipv4=pi.public_ipv4,
                    )
                return True
        return False

    async def _back_to_pending(self, inst_id: uuid.UUID, idx: int, *, release_volume: bool = True) -> None:
        async with self.app.sessionmaker() as s, s.begin():
            await transition_instance(s, inst_id, "creating", "pending", attempt_index=idx)
            if release_volume:
                await s.execute(
                    update(CacheResource).where(CacheResource.attached_instance_id == inst_id)
                    .values(attached_instance_id=None)
                )
                await s.execute(update(Instance).where(Instance.id == inst_id).values(volume_resource_id=None))

    async def _record(self, inst_id: uuid.UUID, offer: GpuOffer, outcome: str, rank: int, err: Exception,
                      t0: float | None = None) -> None:
        async with self.app.sessionmaker() as s, s.begin():
            await record_attempt(
                s, inst_id, offer, outcome, rank=rank, http_status=getattr(err, "http_status", None), error=str(err),
                duration_ms=int((time.monotonic() - t0) * 1000) if t0 else None,
            )

    # ------------------------------------------------------------------ boot

    async def _wait_ip(self, inst: Instance) -> bool:
        rs = await self.app.runtime()
        start = time.monotonic()
        delays = [2, 3, 5, 5, 10]
        i = 0
        while time.monotonic() - start < rs.boot_ip_timeout_s:
            cur = await self._load(inst.id)
            if cur is None or cur.state != "provisioning":
                return False
            assert cur.provider_instance_id
            pi = await self.app.provider.get_instance(cur.provider_instance_id)
            if pi is None:
                await self._fail(inst.id, "The machine disappeared while starting.", requeue=True, code="lost",
                                 outcome="late_failure")
                return False
            if pi.public_ipv4 and pi.status == "active":
                async with self.app.sessionmaker() as s, s.begin():
                    await transition_instance(s, inst.id, "provisioning", "booting",
                                              public_ipv4=pi.public_ipv4, port=pi.port, ip_at=utcnow())
                    if cur.create_accepted_at:
                        secs = (utcnow() - cur.create_accepted_at).total_seconds()
                        await record_phase(s, phase="create", seconds=secs, inst=cur,
                                           estimate_key=f"create:{cur.image_kind}:{cur.size_slug}:{cur.region}")
                return True
            await asyncio.sleep(delays[min(i, len(delays) - 1)] * (self.poll_s / 2.0))
            i += 1
        await self._fail(inst.id, "The machine took too long to start.", requeue=True, code="late_failure",
                         outcome="late_failure")
        return False

    async def _wait_healthy(self, inst: Instance) -> bool:
        rs = await self.app.runtime()
        start = time.monotonic()
        while time.monotonic() - start < rs.boot_health_timeout_s:
            cur = await self._load(inst.id)
            if cur is None or cur.state != "booting":
                return False
            try:
                client = worker_client(self.app, cur)
            except RuntimeError:
                await asyncio.sleep(self.poll_s)
                continue
            try:
                async with client:
                    h = await client.health()
                    if not protocol_compatible(h.protocol):
                        await self._fail(inst.id, "Worker software version mismatch.", requeue=True,
                                         code="boot_failure", outcome="boot_failure")
                        return False
                    if h.engine_ready:
                        new_token = await client.rotate_token()  # the copy in user_data is now useless
                        async with self.app.sessionmaker() as s, s.begin():
                            now = utcnow()
                            await transition_instance(
                                s, inst.id, "booting", "ready",
                                ready_at=now, idle_since=now, last_contact_at=now,
                                max_lifetime_at=now + timedelta(seconds=rs.max_lifetime_s),
                                token_enc=self.app.keyring.seal(new_token, inst.id.bytes),
                                files_present=h.files_present, models_loaded=h.models_loaded,
                            )
                            if cur.ip_at:
                                await record_phase(
                                    s, phase="boot", seconds=(now - cur.ip_at).total_seconds(), inst=cur,
                                    estimate_key=f"boot:{cur.image_kind}:{cur.size_slug}:{cur.region}",
                                )
                        await self._wake_scheduler()
                        return True
            except (WorkerUnreachable, OSError):
                pass
            except Exception as e:  # TLS handshake errors while the agent starts, etc.
                log.debug("health probe for %s failed: %s", inst.id, e)
            await asyncio.sleep(self.poll_s * 1.5)
        await self._fail(inst.id, "The machine started but its software did not come up.", requeue=True,
                         code="boot_failure", outcome="boot_failure")
        return False

    async def _wake_scheduler(self) -> None:
        if self.app.controller is not None:
            self.app.controller.wake()

    # ------------------------------------------------------------------ failure

    async def _fail(
        self, inst_id: uuid.UUID, message: str, *, requeue: bool, code: str = "error", outcome: str | None = None
    ) -> None:
        """Send the machine to destroying (or straight to destroyed if nothing exists) and
        put its waiting jobs back in the queue (or fail them)."""
        app = self.app
        rs = await app.runtime()
        async with app.sessionmaker() as s, s.begin():
            inst = (await s.execute(select(Instance).where(Instance.id == inst_id).with_for_update())).scalar_one_or_none()
            if inst is None:
                return
            if outcome and inst.offer_key and inst.candidates:
                try:
                    offer = GpuOffer(**inst.candidates[inst.attempt_index]["offer"])
                    await record_attempt(s, inst.id, offer, outcome, rank=inst.attempt_index, error=message)
                except (IndexError, KeyError, TypeError):
                    pass
            if inst.provider_instance_id:
                await transition_instance(
                    s, inst.id, tuple(x for x in ("pending", "creating", "provisioning", "booting", "ready", "busy",
                                                  "draining") if x != "destroying"),
                    "destroying", outcome="failed", failure_reason=message, destroy_requested_at=utcnow(),
                )
            else:
                await transition_instance(
                    s, inst.id, ("pending", "creating"), "destroyed", outcome="failed", failure_reason=message,
                )
            await s.execute(
                update(CacheResource).where(CacheResource.attached_instance_id == inst.id)
                .values(attached_instance_id=None)
            )
            jobs = (
                await s.execute(
                    select(Job).where(Job.instance_id == inst.id, Job.state.in_(("provisioning", "queued", "assigned")))
                )
            ).scalars().all()
            now = utcnow()
            for j in jobs:
                if not requeue:
                    await transition_job(s, j.id, j.state, "failed", error_code=code, error_message=message,
                                         stage="failed", instance_id=None)
                    continue
                wait_until = j.capacity_wait_until or now + timedelta(seconds=rs.capacity_wait_max_s)
                if now >= wait_until:
                    await transition_job(s, j.id, j.state, "failed", error_code="no_capacity",
                                         error_message="No GPU machine became available. Please try again later.",
                                         stage="failed", instance_id=None)
                else:
                    backoff = 30 if code not in ("no_capacity", "quota") else min(300, 120 + 60 * j.attempt)
                    await transition_job(
                        s, j.id, j.state, "waiting_capacity" if code in ("no_capacity", "quota") else "queued",
                        instance_id=None, stage="waiting_for_gpu" if code in ("no_capacity", "quota") else "queued",
                        capacity_wait_until=wait_until,
                        next_attempt_at=min(now + timedelta(seconds=backoff), wait_until),
                        error_message=message if code in ("no_capacity", "quota") else None,
                    )
