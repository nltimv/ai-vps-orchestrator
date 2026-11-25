"""RunPod's Serverless GPU types as aivo offers (price per hour of worker time, memory, stock)."""

from __future__ import annotations

import re
from typing import Any

from aivo.config import EnvSettings
from aivo.providers.base import ConfigError, GpuOffer, RegionMeta
from aivo.providers.regions import region_meta, register_regions
from aivo.providers.runpod.api import RunPodAPI

# RunPod GPU names -> aivo GPU classes (catalog/gpu_classes.yaml), most specific first
_GPU_CLASSES: list[tuple[str, str]] = [
    (r"h200", "h200"),
    (r"h100", "h100"),
    (r"b200", "b200"),
    (r"a100", "a100"),
    (r"pro 6000|pro6000", "rtxpro6000"),
    (r"6000 ada", "rtx6000ada"),
    (r"l40s", "l40s"),
    (r"\bl40\b", "l40"),
    (r"a6000", "a6000"),
    (r"\ba40\b", "a40"),
    (r"5090", "rtx5090"),
    (r"4090", "rtx4090"),
    (r"3090", "rtx3090"),
    (r"a5000", "a5000"),
    (r"a4500", "a4500"),
    (r"a4000", "a4000"),
    (r"4000 ada", "rtx4000ada"),
    (r"\bl4\b", "l4"),
]


def gpu_class_of(*names: str | None) -> str | None:
    for n in names:
        low = (n or "").lower()
        for pattern, cls in _GPU_CLASSES:
            if re.search(pattern, low):
                return cls
    return None


def gpu_slug(type_id: str) -> str:
    """Short name for endpoint names: "NVIDIA RTX 6000 Ada Generation" -> "rtx-6000-ada-generation"."""
    s = re.sub(r"^nvidia\s+(geforce\s+)?", "", type_id.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "gpu"


class GpuCatalog:
    """Fetches the GPU types and remembers what creating an endpoint for one needs (pool, data centers)."""

    def __init__(self, api: RunPodAPI, env: EnvSettings):
        self.api = api
        self.env = env
        self.region = env.runpod_region
        self.types: dict[str, dict[str, Any]] = {}  # GPU type id -> catalog entry
        self.pool_types: dict[str, list[str]] = {}  # pool -> GPU type ids
        self.dcs: dict[str, list[str]] = {}  # GPU type id -> data centers with stock
        self._volume_dc: str | None = None
        self._volume_checked = False
        base = region_meta(self.region)
        register_regions({self.region: RegionMeta(self.region, f"{base.name} (RunPod)", base.lat, base.lon)})

    async def volume_dc(self) -> str | None:
        vid = self.env.runpod_network_volume_id
        if not vid:
            return None
        if not self._volume_checked:
            vol = await self.api.network_volume(vid)
            if vol is None:
                raise ConfigError(f"RunPod network volume {vid} not found (AIVO_RUNPOD_NETWORK_VOLUME_ID)")
            self._volume_dc = vol.get("dataCenter") or vol.get("dataCenterId")
            self._volume_checked = True
        return self._volume_dc

    async def offers(self) -> list[GpuOffer]:
        params: dict[str, Any] = {"include": "AVAILABILITY", "product": "SERVERLESS"}
        explicit = [d.strip() for d in self.env.runpod_data_centers if d.strip()]
        if self.env.runpod_countries and not explicit:
            params["countryCodes"] = ",".join(c.strip().upper() for c in self.env.runpod_countries if c.strip())
        gpus = await self.api.gpu_types(params)
        volume_dc = await self.volume_dc()
        allow = {a.strip().lower() for a in self.env.runpod_gpu_types if a.strip()}
        types: dict[str, dict[str, Any]] = {}
        pools: dict[str, list[str]] = {}
        dcs_by_type: dict[str, list[str]] = {}
        out: list[GpuOffer] = []
        for g in gpus:
            tid, pool = g.get("id"), g.get("pool")
            price = (g.get("price") or {}).get("serverless")
            if not tid or not pool or price is None:
                continue
            types[tid] = g
            pools.setdefault(pool, []).append(tid)
            if str(g.get("manufacturer", "NVIDIA")).upper() != "NVIDIA":
                continue  # the worker image is built for CUDA
            if allow and tid.lower() not in allow and str(g.get("name", "")).lower() not in allow:
                continue
            dcs = [
                d["id"]
                for d in g.get("dataCenters") or []
                if d.get("id") and str(d.get("availability", "NONE")).upper() != "NONE"
            ]
            if explicit:
                dcs = [d for d in dcs if d in explicit]
            if volume_dc:
                dcs = [d for d in dcs if d == volume_dc]
            dcs_by_type[tid] = dcs
            vram = float(g.get("memory") or 0)
            out.append(
                GpuOffer(
                    provider="runpod",
                    size_slug=tid[:64],
                    region=self.region,
                    gpu_model=str(g.get("name") or tid),
                    gpu_class=gpu_class_of(g.get("name"), tid),
                    gpu_count=1,
                    vram_gb=vram,
                    vcpus=int(g.get("minVcpuCount") or 0),
                    # host RAM for one GPU; when RunPod doesn't say, assume twice the GPU memory
                    ram_gb=float(g.get("minRam") or 2 * vram),
                    disk_gb=float(self.env.runpod_disk_gb),
                    scratch_gb=0.0,
                    price_hourly=float(price),
                    vendor="nvidia",
                    available=bool(dcs) and str(g.get("availability", "")).upper() != "NONE",
                )
            )
        self.types, self.pool_types, self.dcs = types, pools, dcs_by_type
        return out

    def gpu_selection(self, type_id: str) -> dict[str, Any]:
        """The endpoint's ``gpu`` block: the type's pool minus the pool's other types."""
        g = self.types.get(type_id)
        if g is None:
            raise ConfigError(f"unknown RunPod GPU type {type_id}")
        pool = g["pool"]
        excluded = [t for t in self.pool_types.get(pool, []) if t != type_id]
        return {"pools": [pool], "count": 1, **({"excludedTypes": excluded} if excluded else {})}

    def data_centers(self, type_id: str) -> list[str]:
        return self.dcs.get(type_id) or [d for d in self.env.runpod_data_centers if d]
