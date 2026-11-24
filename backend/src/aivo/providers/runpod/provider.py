"""RunPod Serverless as an aivo machine provider (experimental).

One aivo machine = one RunPod *load-balancing* endpoint with at most one worker:

* create  -> ``POST /v2/serverless`` with the worker image; its settings travel in the endpoint's environment
             (``AIVO_BOOTSTRAP_B64``: ids and the token's hash, never a secret or user content).
             RunPod then prepares a GPU host (pulls the image) - not billed.
* boot    -> aivo's first request through the gateway (``https://<endpoint>.api.runpod.ai``) starts the worker;
             billing runs from here until the endpoint is deleted.
* run     -> the backend talks to the agent exactly as on other providers, through RunPod's gateway (it handles
             HTTPS; requests are pinned to the one worker). Outputs stream back to aivo's own volume.
* destroy -> ``DELETE /v2/serverless/<id>`` terminates the worker: billing stops at once.

If aivo goes silent, RunPod stops the worker after the endpoint's idle timeout anyway.
"""

from __future__ import annotations

import logging
import re
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
    ProviderTraits,
    RegionMeta,
    deployment_tag,
    instance_tag,
    register_traits,
)
from aivo.providers.regions import register_regions
from aivo.providers.runpod.client import RunPodClient
from aivo_protocol.worker_api import BOOTSTRAP_ENV, encode_bootstrap_env

log = logging.getLogger(__name__)

AGENT_PORT = 8000
UNHEALTHY_GIVE_UP_S = 240
# Rough region centres for latency estimates
_REGION_CENTRES = {"eu": (50.1, 8.7), "us": (39.0, -95.0), "ca": (45.5, -73.6)}

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


def _parse_time(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


class RunPodServerlessProvider:
    name = "runpod"

    def __init__(self, env: EnvSettings, *, transport: httpx.AsyncBaseTransport | None = None):
        if env.runpod_api_key is None:
            raise ValueError("AIVO_RUNPOD_API_KEY is required for the runpod provider")
        self.env = env
        self.api = RunPodClient(env.runpod_api_key.get_secret_value(), env.runpod_api_base, transport=transport)
        self.traits = ProviderTraits(
            bills_from_boot=True,
            min_billing_s=0,
            max_idle_s=float(env.runpod_keep_warm_s),
            prepare_timeout_s=float(env.runpod_prepare_timeout_s),
            firewall=False,
            machine_images=False,
            pinned_tls=False,
        )
        register_traits(self.name, self.traits)
        self.region = env.runpod_region
        register_regions(self.regions())
        self._types: dict[str, dict[str, Any]] = {}  # GPU type id -> catalog entry
        self._pool_types: dict[str, list[str]] = {}  # pool -> GPU type ids
        self._dcs: dict[str, list[str]] = {}  # GPU type id -> data centers with stock
        self._volume_dc: str | None = None
        self._volume_checked = False
        self._unhealthy_since: dict[str, float] = {}

    async def aclose(self) -> None:
        await self.api.aclose()

    def regions(self) -> dict[str, RegionMeta]:
        key = self.region.split("-")[0].lower()
        lat, lon = _REGION_CENTRES.get(key, (50.1, 8.7))
        name = {"eu": "Europe", "us": "United States", "ca": "Canada"}.get(key, self.region.upper())
        return {self.region: RegionMeta(self.region, f"{name} (RunPod)", lat, lon, None)}

    def gateway_url(self, endpoint_id: str) -> str:
        return self.env.runpod_gateway_url.format(endpoint_id=endpoint_id)

    # -- offers -----------------------------------------------------------------------

    async def _network_volume_dc(self) -> str | None:
        vid = self.env.runpod_network_volume_id
        if not vid:
            return None
        if not self._volume_checked:
            vol = await self.api.request("GET", f"/v2/network-volumes/{vid}", missing_ok=True)
            if vol is None:
                raise ConfigError(f"RunPod network volume {vid} not found (AIVO_RUNPOD_NETWORK_VOLUME_ID)")
            self._volume_dc = vol.get("dataCenter") or vol.get("dataCenterId")
            self._volume_checked = True
        return self._volume_dc

    async def list_offers(self) -> list[GpuOffer]:
        params: dict[str, Any] = {"include": "AVAILABILITY", "product": "SERVERLESS"}
        explicit = [d.strip() for d in self.env.runpod_data_centers if d.strip()]
        if self.env.runpod_countries and not explicit:
            params["countryCodes"] = ",".join(c.strip().upper() for c in self.env.runpod_countries if c.strip())
        data = await self.api.request("GET", "/v2/catalog/gpus", params=params) or {}
        volume_dc = await self._network_volume_dc()
        allow = {a.strip().lower() for a in self.env.runpod_gpu_types if a.strip()}
        types: dict[str, dict[str, Any]] = {}
        pools: dict[str, list[str]] = {}
        dcs_by_type: dict[str, list[str]] = {}
        offers: list[GpuOffer] = []
        for g in data.get("gpus") or []:
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
            offers.append(
                GpuOffer(
                    provider=self.name,
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
        self._types, self._pool_types, self._dcs = types, pools, dcs_by_type
        return offers

    async def boot_image(self, offer: GpuOffer, strategy: str) -> BootImage:
        # RunPod pulls the worker image onto the host before the worker starts (not billed): like a prepared image
        return BootImage(ref=self.env.worker_image, kind="snapshot")

    # -- machines ---------------------------------------------------------------------

    def endpoint_body(self, spec: InstanceSpec) -> dict[str, Any]:
        tid = spec.offer.size_slug
        g = self._types.get(tid)
        if g is None:
            raise ConfigError(f"unknown RunPod GPU type {tid}")
        pool = g["pool"]
        excluded = [t for t in self._pool_types.get(pool, []) if t != tid]
        boot = spec.bootstrap.model_copy(
            update={
                "listen_port": AGENT_PORT,
                # on the network volume, downloaded model files outlive the worker
                "models_dir": "/runpod-volume/aivo/models"
                if self.env.runpod_network_volume_id
                else "/var/lib/aivo/models",
                # RAM-backed when /dev/shm is big enough (the worker checks)
                "job_dir": "/dev/shm/aivo/jobs",
                "volume_mount": None,
                "tls_cert_pem": "",
                "tls_key_pem": "",
            }
        )
        dcs = self._dcs.get(tid) or [d for d in self.env.runpod_data_centers if d]
        body: dict[str, Any] = {
            "name": spec.name,
            "type": "LOAD_BALANCER",
            "image": spec.worker_image or self.env.worker_image,
            "gpu": {"pools": [pool], "count": 1, **({"excludedTypes": excluded} if excluded else {})},
            "workers": {
                "min": 0,
                "max": 1,
                "idleTimeout": max(5, min(3600, self.env.runpod_idle_timeout_s)),
                "initTimeout": 1800,
            },
            "scaling": {"type": "REQUEST_COUNT", "requestCount": 1},
            "ports": [f"{AGENT_PORT}/http"],
            "disk": self.env.runpod_disk_gb,
            "env": {
                BOOTSTRAP_ENV: encode_bootstrap_env(boot),
                "PORT": str(AGENT_PORT),
                "PORT_HEALTH": str(AGENT_PORT),
                "AIVO_DEPLOYMENT_ID": spec.bootstrap.deployment_id,
                "AIVO_INSTANCE_ID": str(spec.instance_id),
            },
        }
        if dcs:
            body["dataCenterIds"] = dcs
        if self.env.runpod_network_volume_id:
            body["mounts"] = {"network": [{"volumeId": self.env.runpod_network_volume_id}]}
        return body

    async def create_instance(self, spec: InstanceSpec) -> ProviderInstance:
        if spec.offer.size_slug not in self._types:
            await self.list_offers()
        ep = await self.api.request("POST", "/v2/serverless", json=self.endpoint_body(spec), creating=True) or {}
        return self._pi(ep, None)

    def _pi(self, ep: dict[str, Any], workers: dict[str, Any] | None) -> ProviderInstance:
        env = ep.get("env") or {}
        dep, inst = env.get("AIVO_DEPLOYMENT_ID"), env.get("AIVO_INSTANCE_ID")
        tags = ["aivo", "runpod"]
        if dep:
            tags.append(deployment_tag(dep))
        if inst:
            tags.append(instance_tag(inst))
        pools = (ep.get("gpu") or {}).get("pools") or [None]
        pi = ProviderInstance(
            provider_id=str(ep.get("id")),
            name=str(ep.get("name") or ""),
            status="new",
            public_ipv4=None,
            tags=tags,
            created_at=_parse_time(ep.get("createdAt")),
            size_slug=pools[0],
            region=self.region,
            port=443,
        )
        if workers is not None:
            self._apply_workers(pi, workers)
        return pi

    def _apply_workers(self, pi: ProviderInstance, data: dict[str, Any]) -> None:
        """RunPod prepares a worker on a host (pulls the image, not billed) before it can start."""
        states = [str(w.get("status", "")).upper() for w in data.get("workers") or []]
        summary = {k.lower(): v for k, v in (data.get("summary") or {}).items() if isinstance(v, int)}

        def n(state: str) -> int:
            return max(states.count(state.upper()), summary.get(state, 0))

        eid = pi.provider_id
        if n("running") or n("idle"):
            self._unhealthy_since.pop(eid, None)
            pi.status = "active"
            pi.address = self.gateway_url(eid)
            return
        if n("unhealthy"):
            since = self._unhealthy_since.setdefault(eid, time.monotonic())
            pi.stage, pi.note = "starting_worker", "it failed to start; RunPod is retrying"
            if time.monotonic() - since >= UNHEALTHY_GIVE_UP_S:
                pi.failed = (
                    "RunPod could not start the worker software (it keeps failing its health check). "
                    "Check AIVO_WORKER_IMAGE and the endpoint's worker logs in the RunPod console."
                )
            return
        self._unhealthy_since.pop(eid, None)
        if n("throttled") and not n("initializing"):
            pi.stage, pi.note = "waiting_for_gpu", "all of them are busy right now; not billed yet"
        else:
            pi.stage, pi.note = "preparing_host", "downloading the worker software; not billed yet"

    async def get_instance(self, provider_id: str) -> ProviderInstance | None:
        ep = await self.api.request("GET", f"/v2/serverless/{provider_id}", missing_ok=True)
        if ep is None:
            self._unhealthy_since.pop(provider_id, None)
            return None
        workers = await self.api.request("GET", f"/v2/serverless/{provider_id}/workers", missing_ok=True)
        return self._pi(ep, workers or {})

    async def list_instances(self, tag: str) -> list[ProviderInstance]:
        eps = await self.api.paginate("/v2/serverless", "endpoints")
        out = []
        for ep in eps:
            pi = self._pi(ep, None)
            if tag in pi.tags:
                out.append(pi)
        return out

    async def destroy_instance(self, provider_id: str) -> None:
        # deleting the endpoint terminates its worker: billing stops
        await self.api.request("DELETE", f"/v2/serverless/{provider_id}", missing_ok=True)
        self._unhealthy_since.pop(provider_id, None)

    async def ensure_network_access(
        self, tag: str, allowed_cidrs: list[str], port: int, *, ssh_cidrs: list[str] | None = None
    ) -> None:
        return None  # no firewall: the gateway needs the RunPod API key, the worker its own token

    async def account_month_to_date_usd(self) -> float | None:
        return None

    async def gpu_quota(self) -> int | None:
        return None
