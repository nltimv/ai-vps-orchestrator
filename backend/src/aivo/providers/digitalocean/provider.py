"""DigitalOcean GPU Droplets provider."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import httpx

from aivo.config import EnvSettings
from aivo.providers.base import (
    BootImage,
    ConfigError,
    GpuOffer,
    InstanceSpec,
    ProviderInstance,
    RegionMeta,
    parse_size_slug,
)
from aivo.providers.digitalocean.client import DOClient
from aivo.providers.digitalocean.cloudinit import render_user_data
from aivo.providers.regions import DO_REGIONS

log = logging.getLogger(__name__)

# Used only if the API lists a GPU size with an empty region list (a known quirk).
FALLBACK_REGIONS = {
    "gpu-4000adax1-20gb": ["tor1"],
    "gpu-6000adax1-48gb": ["tor1"],
    "gpu-l40sx1-48gb": ["tor1"],
    "gpu-h100x1-80gb": ["nyc2", "tor1", "ams3"],
    "gpu-h200x1-141gb": ["nyc2", "atl1"],
    "gpu-mi300x1-192gb": ["atl1"],
}
# CPU-only helper machines (filling a model disk). GPU machines use AIVO_DO_BASE_IMAGE(_AMD).
FALLBACK_BASE_IMAGE = "ubuntu-24-04-x64"
GPU_TOKENS = {
    "4000ada": "rtx4000ada",
    "6000ada": "rtx6000ada",
    "l40s": "l40s",
    "h100": "h100",
    "h200": "h200",
    "mi300x": "mi300x",
    "mi325x": "mi325x",
    "mi300": "mi300x",
    "mi325": "mi325x",
}
GPU_LABELS = {
    "rtx4000ada": "RTX 4000 Ada",
    "rtx6000ada": "RTX 6000 Ada",
    "l40s": "L40S",
    "h100": "H100",
    "h200": "H200",
    "mi300x": "MI300X",
    "mi325x": "MI325X",
}


def _gib(info: dict[str, Any] | None) -> float:
    if not info:
        return 0.0
    amount = float(info.get("amount", 0))
    unit = str(info.get("unit", "gib")).lower()
    return amount * 1024 if unit.startswith("t") else amount


def parse_offers(sizes: list[dict[str, Any]], provider: str = "digitalocean") -> list[GpuOffer]:
    out: list[GpuOffer] = []
    for sz in sizes:
        slug = sz.get("slug", "")
        gi = sz.get("gpu_info") or {}
        if not gi and not slug.startswith("gpu-"):
            continue
        parsed = parse_size_slug(slug) or {}
        token = str(parsed.get("model", "")).lower()
        gpu_class = GPU_TOKENS.get(token)
        model_name = str(gi.get("model") or "")
        if gpu_class is None and model_name:
            low = model_name.lower().replace(" ", "_")
            gpu_class = next((v for k, v in GPU_TOKENS.items() if k in low), None)
        count = int(gi.get("count") or parsed.get("count") or 1)
        # vram_gb is per GPU. The slug carries the total (e.g. gpu-h100x8-640gb), which is unambiguous.
        slug_vram = float(parsed.get("vram") or 0)
        vram = slug_vram / count if slug_vram else _gib(gi.get("vram"))
        vendor = "amd" if (gpu_class or "").startswith("mi") or "amd" in model_name.lower() else "nvidia"
        disk = float(sz.get("disk") or 0)
        scratch = 0.0
        for d in sz.get("disk_info") or []:
            if d.get("type") == "scratch":
                scratch += _gib(d.get("size"))
            elif d.get("type") == "local" and not disk:
                disk = _gib(d.get("size"))
        regions = list(sz.get("regions") or [])
        known = bool(regions)
        if not known:
            regions = FALLBACK_REGIONS.get(slug, [])
        label = GPU_LABELS.get(gpu_class or "", model_name or slug)
        for region in regions:
            out.append(
                GpuOffer(
                    provider=provider,
                    size_slug=slug,
                    region=region,
                    gpu_model=label if count == 1 else f"{count}x {label}",
                    gpu_class=gpu_class,
                    gpu_count=count,
                    vram_gb=vram,
                    vcpus=int(sz.get("vcpus") or 0),
                    ram_gb=float(sz.get("memory") or 0) / 1024,
                    disk_gb=disk,
                    scratch_gb=scratch,
                    price_hourly=float(sz.get("price_hourly") or 0),
                    vendor=vendor,  # type: ignore[arg-type]
                    available=bool(sz.get("available", True)),
                    regions_known=known,
                )
            )
    return out


def _pi(d: dict[str, Any], port: int) -> ProviderInstance:
    ip = None
    for n in (d.get("networks") or {}).get("v4", []):
        if n.get("type") == "public":
            ip = n.get("ip_address")
    created = d.get("created_at")
    return ProviderInstance(
        provider_id=str(d["id"]),
        name=d.get("name", ""),
        status=d.get("status", "unknown"),
        public_ipv4=ip,
        tags=list(d.get("tags") or []),
        created_at=datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None,
        size_slug=d.get("size_slug") or (d.get("size") or {}).get("slug"),
        region=(d.get("region") or {}).get("slug"),
        port=port,
    )


class DigitalOceanProvider:
    name = "digitalocean"

    def __init__(self, env: EnvSettings, *, transport: httpx.AsyncBaseTransport | None = None):
        assert env.do_token is not None
        self.env = env
        self.api = DOClient(env.do_token.get_secret_value(), env.do_api_base, transport=transport)
        self.port = env.worker_port
        self._ssh_key_id: int | None = None
        self._fw_cache: tuple[float, tuple[str, ...]] | None = None

    async def aclose(self) -> None:
        await self.api.aclose()

    def regions(self) -> dict[str, RegionMeta]:
        return DO_REGIONS

    async def list_offers(self) -> list[GpuOffer]:
        sizes = await self.api.paginate("/v2/sizes", "sizes")
        return parse_offers(sizes)

    # -- images ---------------------------------------------------------------------

    async def boot_image(self, offer: GpuOffer, strategy: str) -> BootImage:
        """Base GPU image (with the GPU driver). Prepared snapshots are chosen by the caller from the cache table.

        Deliberately no fallback to a plain OS image: without a GPU driver the machine can never run
        the AI engine, so a wrong slug must fail loudly (ConfigError) instead of burning GPU hours.
        DigitalOcean documents gpu-h100x1-base for all single-GPU NVIDIA sizes, gpu-amd-base for AMD.
        """
        ref = self.env.do_base_image_amd if offer.vendor == "amd" else self.env.do_base_image
        return BootImage(ref=ref, kind="base")

    # -- machines -------------------------------------------------------------------

    async def ensure_ssh_key(self) -> int:
        """Register a throw-away public key so DigitalOcean never emails a root password.
        The private key is discarded immediately (we never SSH in)."""
        if self._ssh_key_id is not None:
            return self._ssh_key_id
        name = "aivo-no-login"
        keys = await self.api.paginate("/v2/account/keys", "ssh_keys")
        for k in keys:
            if k.get("name") == name:
                self._ssh_key_id = int(k["id"])
                return self._ssh_key_id
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        pub = (
            Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )
        r = await self.api.request("POST", "/v2/account/keys", json={"name": name, "public_key": pub})
        self._ssh_key_id = int(r["ssh_key"]["id"])
        return self._ssh_key_id

    async def _ssh_keys(self) -> list[int | str]:
        keys: list[int | str] = [await self.ensure_ssh_key()]
        keys += [int(k) if k.isdigit() else k for k in self.env.do_ssh_key_ids if k.strip()]
        return keys

    async def create_instance(self, spec: InstanceSpec) -> ProviderInstance:
        body: dict[str, Any] = {
            "name": spec.name,
            "region": spec.offer.region,
            "size": spec.offer.size_slug,
            "image": int(spec.image.ref) if spec.image.ref.isdigit() else spec.image.ref,
            "ssh_keys": await self._ssh_keys(),
            "tags": spec.tags,
            "user_data": render_user_data(spec.bootstrap),
            "monitoring": False,
            "ipv6": False,
        }
        if spec.volume_ids:
            body["volumes"] = spec.volume_ids
        r = await self.api.request("POST", "/v2/droplets", json=body, creating=True)
        return _pi(r["droplet"], self.port)

    async def get_instance(self, provider_id: str) -> ProviderInstance | None:
        from aivo.providers.base import ConfigError as _CE

        try:
            r = await self.api.request("GET", f"/v2/droplets/{provider_id}")
        except _CE as e:
            if e.http_status == 404:
                return None
            raise
        return _pi(r["droplet"], self.port)

    async def list_instances(self, tag: str) -> list[ProviderInstance]:
        ds = await self.api.paginate("/v2/droplets", "droplets", {"tag_name": tag})
        return [_pi(d, self.port) for d in ds]

    async def destroy_instance(self, provider_id: str) -> None:
        # plain delete: never "destroy with associated resources" (would take the cache volume)
        try:
            await self.api.request("DELETE", f"/v2/droplets/{provider_id}", ok=(202, 204))
        except ConfigError as e:
            if e.http_status != 404:
                raise

    async def create_bake_instance(self, offer: GpuOffer, user_data: str, tags: list[str]) -> ProviderInstance:
        img = await self.boot_image(offer, "base")
        r = await self.api.request(
            "POST",
            "/v2/droplets",
            creating=True,
            json={
                "name": f"aivo-bake-{int(time.time())}",
                "region": offer.region,
                "size": offer.size_slug,
                "image": img.ref,
                "ssh_keys": await self._ssh_keys(),
                "tags": tags,
                "user_data": user_data,
                "monitoring": False,
            },
        )
        return _pi(r["droplet"], self.port)

    async def create_utility_instance(
        self, region: str, user_data: str, volume_ids: list[str], tags: list[str], size: str = "s-2vcpu-4gb"
    ) -> ProviderInstance:
        """Small CPU machine for maintenance (filling a cache volume). Powers itself off when done."""
        ssh_key = await self.ensure_ssh_key()
        r = await self.api.request(
            "POST",
            "/v2/droplets",
            creating=True,
            json={
                "name": f"aivo-util-{int(time.time())}",
                "region": region,
                "size": size,
                "image": FALLBACK_BASE_IMAGE,
                "ssh_keys": [ssh_key],
                "tags": tags,
                "user_data": user_data,
                "volumes": volume_ids,
                "monitoring": False,
            },
        )
        return _pi(r["droplet"], self.port)

    async def power_off(self, provider_id: str) -> int:
        r = await self.api.request("POST", f"/v2/droplets/{provider_id}/actions", json={"type": "power_off"})
        return int(r["action"]["id"])

    async def snapshot(self, provider_id: str, name: str) -> int:
        r = await self.api.request(
            "POST", f"/v2/droplets/{provider_id}/actions", json={"type": "snapshot", "name": name}
        )
        return int(r["action"]["id"])

    async def action_status(self, action_id: int) -> str:
        r = await self.api.request("GET", f"/v2/actions/{action_id}")
        return str(r["action"]["status"])  # in-progress | completed | errored

    async def wait_action(self, action_id: int, timeout_s: float = 3600, poll_s: float = 10) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            st = await self.action_status(action_id)
            if st == "completed":
                return
            if st == "errored":
                raise ConfigError(f"action {action_id} errored")
            await asyncio.sleep(poll_s)
        raise TimeoutError(f"action {action_id} timed out")

    async def find_snapshot(self, name: str) -> dict[str, Any] | None:
        snaps = await self.api.paginate("/v2/snapshots", "snapshots", {"resource_type": "droplet"})
        return next((s for s in snaps if s.get("name") == name), None)

    async def transfer_image(self, image_id: str, region: str) -> int:
        r = await self.api.request(
            "POST", f"/v2/images/{image_id}/actions", json={"type": "transfer", "region": region}
        )
        return int(r["action"]["id"])

    async def get_image(self, image_id: str) -> dict[str, Any] | None:
        try:
            return (await self.api.request("GET", f"/v2/images/{image_id}"))["image"]  # type: ignore[no-any-return]
        except ConfigError as e:
            if e.http_status == 404:
                return None
            raise

    async def delete_image(self, image_id: str) -> None:
        try:
            await self.api.request("DELETE", f"/v2/images/{image_id}", ok=(204,))
        except ConfigError as e:
            if e.http_status != 404:
                raise

    # -- cache volumes ----------------------------------------------------------------

    async def create_volume(self, name: str, region: str, size_gb: int, tags: list[str]) -> dict[str, Any]:
        r = await self.api.request(
            "POST",
            "/v2/volumes",
            json={
                "name": name,
                "region": region,
                "size_gigabytes": size_gb,
                "filesystem_type": "ext4",
                "filesystem_label": "aivocache",
                "tags": tags,
                "description": "aivo model-weight cache (public model files only)",
            },
        )
        return r["volume"]  # type: ignore[no-any-return]

    async def delete_volume(self, volume_id: str) -> None:
        try:
            await self.api.request("DELETE", f"/v2/volumes/{volume_id}", ok=(204,))
        except ConfigError as e:
            if e.http_status != 404:
                raise

    # -- network ----------------------------------------------------------------------

    async def ensure_network_access(
        self, tag: str, allowed_cidrs: list[str], port: int, *, ssh_cidrs: list[str] | None = None
    ) -> None:
        """One Cloud Firewall per deployment, attached by tag, so new machines are covered from
        the moment they are created. ``ssh_cidrs`` (debugging only) also opens port 22."""
        sources = tuple(sorted(allowed_cidrs)) or ("0.0.0.0/0", "::/0")
        ssh = tuple(sorted(c for c in (ssh_cidrs or []) if c.strip()))
        key = (*sources, "ssh:", *ssh)
        if self._fw_cache and self._fw_cache[1] == key and time.monotonic() - self._fw_cache[0] < 300:
            return
        name = f"aivo-{tag[len('aivo-dep-') :][:30]}"
        inbound = [{"protocol": "tcp", "ports": str(port), "sources": {"addresses": list(sources)}}]
        if ssh:
            inbound.append({"protocol": "tcp", "ports": "22", "sources": {"addresses": list(ssh)}})
        body = {
            "name": name,
            "inbound_rules": inbound,
            "outbound_rules": [
                {
                    "protocol": p,
                    "ports": "all" if p != "icmp" else None,
                    "destinations": {"addresses": ["0.0.0.0/0", "::/0"]},
                }
                for p in ("tcp", "udp", "icmp")
            ],
            "tags": [tag],
        }
        for r in body["outbound_rules"]:  # type: ignore[union-attr]
            if r["ports"] is None:
                del r["ports"]
        fws = await self.api.paginate("/v2/firewalls", "firewalls")
        existing = next((f for f in fws if f.get("name") == name), None)
        if existing:
            await self.api.request("PUT", f"/v2/firewalls/{existing['id']}", json=body)
        else:
            await self.api.request("POST", "/v2/firewalls", json=body)
        self._fw_cache = (time.monotonic(), key)

    # -- account ----------------------------------------------------------------------

    async def account_month_to_date_usd(self) -> float | None:
        try:
            r = await self.api.request("GET", "/v2/customers/my/balance")
            return float(r.get("month_to_date_usage") or 0)
        except Exception:
            return None

    async def gpu_quota(self) -> int | None:
        try:
            r = await self.api.request("GET", "/v2/account")
            return int(r["account"].get("droplet_limit"))
        except Exception:
            return None


async def detect_egress_ip() -> str | None:
    """The backend's public IPv4 as seen from the internet (for the firewall rule)."""
    for url in ("https://api.ipify.org", "https://ipv4.icanhazip.com"):
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                ip = (await c.get(url)).text.strip()
                if ip.count(".") == 3:
                    return ip
        except httpx.HTTPError:
            continue
    return None
