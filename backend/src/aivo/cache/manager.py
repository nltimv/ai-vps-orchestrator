"""Weight cache management: the Spaces mirror and region cache volumes.

* Mirror: streams model files from Hugging Face straight into the private
  Spaces bucket (S3 multipart upload, never buffered whole). Runs in the
  backend process: fastest when the backend itself runs inside DigitalOcean.
* Volumes: one block-storage volume per region listed in the cache policy,
  filled by a short-lived CPU machine that copies the "hot" models' files from
  the mirror (or Hugging Face) and is destroyed right after.

Only public model files ever go here. Generated content never does.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aivo.cache.spaces import SpacesMirror, hf_url
from aivo.context import AppContext
from aivo.controller.common import utcnow
from aivo.db.models import BackgroundRun, CacheResource, ModelState
from aivo.events.bus import emit
from aivo.providers.base import deployment_tag
from aivo_protocol.catalog_types import FileSpec

log = logging.getLogger(__name__)

PART = 64 * 1024 * 1024
VOLUME_PRICE_GB_MONTH = 0.10


class CacheManager:
    def __init__(self, app: AppContext):
        self.app = app
        self._mirror: SpacesMirror | None = None
        if app.env.spaces_bucket:
            try:
                self._mirror = SpacesMirror(app.env)
            except ValueError:
                pass

    # ------------------------------------------------------------------ status

    async def status(self) -> dict[str, Any]:
        async with self.app.sessionmaker() as s:
            res = (
                (
                    await s.execute(
                        select(CacheResource)
                        .where(CacheResource.status.notin_(("deleted",)))
                        .order_by(CacheResource.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            models = {m.model_id: m for m in (await s.execute(select(ModelState))).scalars().all()}
            runs = (
                (await s.execute(select(BackgroundRun).order_by(BackgroundRun.started_at.desc()).limit(10)))
                .scalars()
                .all()
            )
        mirrored_gb = sum(m.mirrored_bytes for m in models.values()) / 1e9
        return {
            "resources": [
                {
                    "id": str(r.id),
                    "kind": r.kind,
                    "region": r.region,
                    "status": r.status,
                    "size_gb": r.size_gb,
                    "monthly_cost_usd": r.monthly_cost_est,
                    "contents": len(r.contents or []),
                    "in_use": r.attached_instance_id is not None,
                    "created_at": r.created_at,
                }
                for r in res
            ],
            "mirror": {
                "configured": self._mirror is not None,
                "stored_gb": round(mirrored_gb, 1),
                "monthly_cost_usd": self._mirror.monthly_cost_estimate(mirrored_gb) if self._mirror else 0,
                "models": {
                    mid: {"status": m.mirror_status, "gb": round(m.mirrored_bytes / 1e9, 1)}
                    for mid, m in models.items()
                },
            },
            "runs": [
                {
                    "id": str(r.id),
                    "kind": r.kind,
                    "state": r.state,
                    "step": r.step,
                    "stage": (r.data or {}).get("stage"),  # what the bake machine is doing right now
                    "error": (r.data or {}).get("error"),
                    "log_tail": (r.data or {}).get("log_tail") if r.state == "failed" else None,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                }
                for r in runs
            ],
            "monthly_total_usd": round(
                sum(r.monthly_cost_est for r in res if r.status != "deprecated")
                + (self._mirror.monthly_cost_estimate(mirrored_gb) if self._mirror else 0),
                2,
            ),
        }

    # ------------------------------------------------------------------ mirror

    def _model_files(self, model_ids: list[str]) -> list[FileSpec]:
        seen: dict[str, FileSpec] = {}
        for mid in model_ids:
            mf = self.app.catalog.get(mid).manifest
            for f in mf.files:
                seen.setdefault(SpacesMirror.key_for(f), f)
        return list(seen.values())

    async def mirror_models(self, model_ids: list[str]) -> None:
        if self._mirror is None:
            raise RuntimeError("the Spaces mirror is not configured")
        for mid in model_ids:
            await self._set_model(mid, status="mirroring")
            total = 0
            try:
                for f in self._model_files([mid]):
                    have = await asyncio.to_thread(self._mirror.exists, f)
                    if have and (not f.sha256 or have == f.bytes):
                        total += have
                        continue
                    total += await self._stream_to_spaces(f)
                await self._set_model(mid, status="ready", bytes_=total)
            except Exception as e:
                log.exception("mirroring %s failed", mid)
                await self._set_model(mid, status="failed")
                raise RuntimeError(f"mirroring {mid} failed: {e}") from e

    async def _set_model(self, mid: str, *, status: str, bytes_: int | None = None) -> None:
        async with self.app.sessionmaker() as s, s.begin():
            vals: dict[str, Any] = {"model_id": mid, "mirror_status": status}
            if bytes_ is not None:
                vals.update(mirrored_bytes=bytes_, mirrored_at=utcnow())
            await s.execute(
                insert(ModelState)
                .values(**vals)
                .on_conflict_do_update(
                    index_elements=[ModelState.model_id], set_={k: v for k, v in vals.items() if k != "model_id"}
                )
            )

    async def _stream_to_spaces(self, f: FileSpec) -> int:
        assert self._mirror is not None
        client = self._mirror.client
        key = SpacesMirror.key_for(f)
        headers = {}
        if f.gated and self.app.env.hf_token:
            headers["Authorization"] = f"Bearer {self.app.env.hf_token.get_secret_value()}"
        mpu = await asyncio.to_thread(client.create_multipart_upload, Bucket=self._mirror.bucket, Key=key)
        parts: list[dict[str, Any]] = []
        size = 0
        try:
            async with (
                httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, read=600)) as c,
                c.stream("GET", hf_url(f), headers=headers) as r,
            ):
                r.raise_for_status()
                buf = bytearray()
                n = 1
                async for chunk in r.aiter_bytes(1 << 20):
                    buf.extend(chunk)
                    if len(buf) >= PART:
                        etag = await asyncio.to_thread(self._upload_part, key, mpu["UploadId"], n, bytes(buf))
                        parts.append({"PartNumber": n, "ETag": etag})
                        size += len(buf)
                        buf.clear()
                        n += 1
                if buf or n == 1:
                    etag = await asyncio.to_thread(self._upload_part, key, mpu["UploadId"], n, bytes(buf))
                    parts.append({"PartNumber": n, "ETag": etag})
                    size += len(buf)
            await asyncio.to_thread(
                client.complete_multipart_upload,
                Bucket=self._mirror.bucket,
                Key=key,
                UploadId=mpu["UploadId"],
                MultipartUpload={"Parts": parts},
            )
            return size
        except BaseException:
            await asyncio.to_thread(
                client.abort_multipart_upload, Bucket=self._mirror.bucket, Key=key, UploadId=mpu["UploadId"]
            )
            raise

    def _upload_part(self, key: str, upload_id: str, n: int, data: bytes) -> str:
        assert self._mirror is not None
        r = self._mirror.client.upload_part(
            Bucket=self._mirror.bucket, Key=key, UploadId=upload_id, PartNumber=n, Body=data
        )
        return str(r["ETag"])

    # ------------------------------------------------------------------ volumes

    async def ensure_volumes(self) -> list[str]:
        """Create (empty) cache volumes for the regions in the cache policy."""
        rs = await self.app.runtime(fresh=True)
        p: Any = self.app.provider
        created: list[str] = []
        if not rs.cache_policy.volume_enabled or not hasattr(p, "create_volume"):
            return created
        async with self.app.sessionmaker() as s:
            have = {
                r.region
                for r in (
                    await s.execute(
                        select(CacheResource).where(
                            CacheResource.kind == "volume", CacheResource.status.notin_(("deleted", "failed"))
                        )
                    )
                )
                .scalars()
                .all()
            }
        for region in rs.cache_policy.volume_regions:
            if region in have:
                continue
            name = f"aivo-cache-{region}-{uuid.uuid4().hex[:6]}"
            vol = await p.create_volume(
                name, region, rs.cache_policy.volume_size_gb, ["aivo", deployment_tag(self.app.deployment_id)]
            )
            async with self.app.sessionmaker() as s, s.begin():
                s.add(
                    CacheResource(
                        provider=self.app.provider.name,
                        kind="volume",
                        region=region,
                        provider_resource_id=str(vol["id"]),
                        name=name,
                        size_gb=rs.cache_policy.volume_size_gb,
                        status="empty",
                        monthly_cost_est=round(rs.cache_policy.volume_size_gb * VOLUME_PRICE_GB_MONTH, 2),
                    )
                )
                await emit(s, scope="system", type="cache.volume_created", payload={"region": region})
            created.append(region)
        return created

    def fill_script(self, files: list[tuple[str, str, int | None]]) -> str:
        """cloud-init for a throw-away CPU machine that fills a cache volume, then powers off."""
        manifest = base64.b64encode(json.dumps(files).encode()).decode()
        script = f"""#!/bin/bash
set -euo pipefail
exec >>/var/log/aivo-fill.log 2>&1
DEV=$(ls /dev/disk/by-id/scsi-0DO_Volume_* | head -n1)
mkdir -p /mnt/cache && mount -o noatime "$DEV" /mnt/cache
echo '{manifest}' | base64 -d > /tmp/files.json
python3 - <<'PY'
import json, os, subprocess
for dest, url, size in json.load(open('/tmp/files.json')):
    p = os.path.join('/mnt/cache', dest)
    if os.path.exists(p) and (size is None or os.path.getsize(p) == size):
        continue
    os.makedirs(os.path.dirname(p), exist_ok=True)
    subprocess.run(['curl', '-fL', '--retry', '5', '-o', p + '.part', url], check=True)
    os.replace(p + '.part', p)
PY
sync
touch /mnt/cache/.aivo-filled
umount /mnt/cache
poweroff
"""
        return (
            "#cloud-config\nwrite_files:\n- path: /opt/aivo/fill.sh\n  permissions: '0755'\n  encoding: b64\n"
            f"  content: {base64.b64encode(script.encode()).decode()}\nruncmd:\n- [bash, /opt/aivo/fill.sh]\n"
        )

    def fill_files(self, model_ids: list[str]) -> list[tuple[str, str, int | None]]:
        out = []
        for f in self._model_files(model_ids):
            url = self._mirror.presign_get(f, expires_s=6 * 3600) if self._mirror else hf_url(f)
            out.append((f.dest, url, f.bytes if f.sha256 else None))
        return out

    async def fill_volume(self, region: str, model_ids: list[str], *, poll_s: float = 15.0) -> None:
        """Copy the given models' files onto the region's cache volume using a small CPU machine."""
        p: Any = self.app.provider
        if not hasattr(p, "create_utility_instance"):
            raise RuntimeError("this provider can't fill cache volumes")
        claim = uuid.uuid4()
        async with self.app.sessionmaker() as s, s.begin():
            vol = (
                await s.execute(
                    select(CacheResource)
                    .where(
                        CacheResource.kind == "volume",
                        CacheResource.region == region,
                        CacheResource.status.in_(("empty", "ready", "verified")),
                        CacheResource.attached_instance_id.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if vol is None:
                raise RuntimeError(f"no free cache volume in {region}")
            vol.attached_instance_id = claim
            vol.status = "filling"
            vol_id, vol_row = vol.provider_resource_id, vol.id
        pid = None
        try:
            files = self.fill_files(model_ids)
            pi = await p.create_utility_instance(
                region, self.fill_script(files), [vol_id], ["aivo", deployment_tag(self.app.deployment_id), "aivo-fill"]
            )
            pid = pi.provider_id
            for _ in range(int(4 * 3600 / poll_s)):
                cur = await p.get_instance(pid)
                if cur is None or cur.status == "off":
                    break
                await asyncio.sleep(poll_s)
            else:
                raise TimeoutError("filling the cache volume took too long")
            dests = [d for d, _, _ in files]
            async with self.app.sessionmaker() as s, s.begin():
                v = (await s.execute(select(CacheResource).where(CacheResource.id == vol_row))).scalar_one()
                v.contents = sorted(set(v.contents or []) | set(dests))
                v.status = "ready"
                v.verified_at = utcnow()
        except BaseException:
            async with self.app.sessionmaker() as s, s.begin():
                v = (await s.execute(select(CacheResource).where(CacheResource.id == vol_row))).scalar_one()
                v.status = "ready" if v.contents else "empty"
            raise
        finally:
            if pid:
                try:
                    await p.destroy_instance(pid)
                except Exception as e:
                    log.warning("could not destroy fill machine %s: %s", pid, e)
            async with self.app.sessionmaker() as s, s.begin():
                v = (await s.execute(select(CacheResource).where(CacheResource.id == vol_row))).scalar_one()
                if v.attached_instance_id == claim:
                    v.attached_instance_id = None
