"""RunPod's Serverless GPU types as aivo offers (price per hour of worker time, memory, stock)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from aivo.config import EnvSettings
from aivo.providers.base import ConfigError, GpuOffer, RegionMeta
from aivo.providers.regions import region_meta, register_regions
from aivo.providers.runpod.api import RunPodAPI

log = logging.getLogger(__name__)

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


def dc_country(dc_id: str) -> str | None:
    """The country of a RunPod data center, from its id: EU-NL-1 -> NL, EUR-IS-2 -> IS, US-CA-2 -> US (California),
    CA-MTL-1 -> CA. RunPod's data center list has no country field."""
    parts = dc_id.upper().split("-")
    if len(parts) < 2:
        return None
    if parts[0] in ("US", "CA"):
        return parts[0]
    if len(parts[1]) == 2:
        return parts[1]
    return parts[0] if len(parts[0]) == 2 else None


SLUG_MAX = 40


def gpu_slug(type_id: str) -> str:
    """Short name for endpoint names: "NVIDIA RTX 6000 Ada Generation" -> "rtx-6000-ada-generation".

    Too long a name loses middle words, longest first, rather than its end: cutting it off gave both
    "RTX PRO 6000 Blackwell Server Edition MIG 1g.24gb" and "... MIG 2g.48gb" the same name.
    """
    s = re.sub(r"^nvidia\s+(geforce\s+)?", "", type_id.lower())
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w]
    while len("-".join(words)) > SLUG_MAX and len(words) > 4:
        middle = range(2, len(words) - 2)
        words.pop(max(middle, key=lambda i: (len(words[i]), i)))
    return "-".join(words)[:SLUG_MAX].strip("-") or "gpu"


CATALOG_MAX_AGE_S = 3600  # the full GPU list and the data center list change rarely


class GpuCatalog:
    """Fetches the GPU types and remembers what creating an endpoint for one needs (pool, data centers).

    An endpoint's settings must not follow stock: every change makes a new RunPod release, and its workers start
    over. So the pool's other GPU types come from the full catalog (the country-filtered one lists only types
    with stock right now), and endpoints may use every data center in the allowed countries (RunPod starts
    workers only where there is stock anyway).
    """

    def __init__(self, api: RunPodAPI, env: EnvSettings):
        self.api = api
        self.env = env
        self.region = env.runpod_region
        self.types: dict[str, dict[str, Any]] = {}  # GPU type id -> catalog entry
        self.pool_types: dict[str, list[str]] = {}  # pool -> all GPU type ids in it, with stock or not
        self.dcs: dict[str, list[str]] = {}  # GPU type id -> data centers with stock (for availability)
        self.allowed_dcs: list[str] = []  # where endpoints may run: stable, sorted
        self._seen_dcs: set[str] = set()
        self._all: list[dict[str, Any]] = []
        self._dc_ids: list[str] = []
        self._fetched_at = -1e18
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

    async def _stable_catalogs(self) -> None:
        """Every GPU type (for pools) and every data center, refreshed hourly; failures keep the last lists."""
        if time.monotonic() - self._fetched_at < CATALOG_MAX_AGE_S:
            return
        try:
            self._all = await self.api.gpu_types({})
        except Exception as e:
            log.info("could not list all RunPod GPU types: %s", e)
        try:
            self._dc_ids = sorted({str(d["id"]) for d in await self.api.data_centers() if d.get("id")})
        except Exception as e:
            log.info("could not list RunPod data centers: %s", e)
        self._fetched_at = time.monotonic()

    def _allowed(self, explicit: list[str], volume_dc: str | None, stocked: set[str]) -> list[str]:
        if volume_dc:
            return [volume_dc]
        if explicit:
            return sorted(set(explicit))
        countries = {c.strip().upper() for c in self.env.runpod_countries if c.strip()}
        picked = {d for d in self._dc_ids if not countries or dc_country(d) in countries}
        # RunPod filtered the stock list by country itself, so a data center with stock is allowed; and one that
        # was allowed stays in the list when its stock runs out (no change, no new release)
        self._seen_dcs |= picked | stocked
        return sorted(self._seen_dcs)

    async def offers(self) -> list[GpuOffer]:
        params: dict[str, Any] = {"include": "AVAILABILITY", "product": "SERVERLESS"}
        explicit = [d.strip() for d in self.env.runpod_data_centers if d.strip()]
        if self.env.runpod_countries and not explicit:
            params["countryCodes"] = ",".join(c.strip().upper() for c in self.env.runpod_countries if c.strip())
        gpus = await self.api.gpu_types(params)
        await self._stable_catalogs()
        volume_dc = await self.volume_dc()
        allow = {a.strip().lower() for a in self.env.runpod_gpu_types if a.strip()}
        types: dict[str, dict[str, Any]] = {}
        pools: dict[str, list[str]] = {}
        for g in self._all:
            if g.get("id") and g.get("pool"):
                types[g["id"]] = g
                pools.setdefault(g["pool"], []).append(g["id"])
        dcs_by_type: dict[str, list[str]] = {}
        out: list[GpuOffer] = []
        for g in gpus:
            tid, pool = g.get("id"), g.get("pool")
            price = (g.get("price") or {}).get("serverless")
            if not tid or not pool or price is None:
                continue
            types[tid] = g
            if tid not in pools.setdefault(pool, []):
                pools[pool].append(tid)
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
        stocked = {d for ds in dcs_by_type.values() for d in ds}
        self.types, self.pool_types, self.dcs = types, pools, dcs_by_type
        self.allowed_dcs = self._allowed(explicit, volume_dc, stocked)
        return out

    def gpu_selection(self, type_id: str) -> dict[str, Any]:
        """The endpoint's ``gpu`` block: the type's pool minus the pool's other types."""
        g = self.types.get(type_id)
        if g is None:
            raise ConfigError(f"unknown RunPod GPU type {type_id}")
        pool = g["pool"]
        excluded = sorted(t for t in self.pool_types.get(pool, []) if t != type_id)
        return {"pools": [pool], "count": 1, **({"excludedTypes": excluded} if excluded else {})}

    def data_centers(self, type_id: str) -> list[str]:
        """Where this GPU type's endpoint may run: the same stable list for every type (see the class)."""
        return self.allowed_dcs or sorted(self.dcs.get(type_id) or [d for d in self.env.runpod_data_centers if d])
