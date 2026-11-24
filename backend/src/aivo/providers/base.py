"""Cloud provider abstraction.

Everything cloud-specific lives behind ``CloudProvider``. The scheduler only
sees normalized ``GpuOffer`` objects and a small error taxonomy that drives
fallback and idempotency:

* ``CapacityError``   - this size/region is sold out right now -> try next candidate
* ``VolumeBusy``      - cache volume still attached elsewhere -> retry without it
* ``QuotaError``      - account limit reached -> stop provisioning, tell the admin
* ``RateLimited``     - API rate limit -> pause until ``reset_at``
* ``UnknownOutcome``  - request may or may not have created a machine -> look up by tag
* ``TransientError``  - retry later
* ``AuthError`` / ``ConfigError`` - operator must fix configuration
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from aivo_protocol.worker_api import WorkerBootstrap


class ProviderError(Exception):
    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class CapacityError(ProviderError):
    pass


class VolumeBusy(ProviderError):
    pass


class QuotaError(ProviderError):
    pass


class RateLimited(ProviderError):
    def __init__(self, message: str, *, reset_at: float | None = None, http_status: int | None = 429):
        super().__init__(message, http_status=http_status)
        self.reset_at = reset_at


class UnknownOutcome(ProviderError):
    pass


class TransientError(ProviderError):
    pass


class AuthError(ProviderError):
    pass


class ConfigError(ProviderError):
    pass


@dataclass(frozen=True)
class RegionMeta:
    slug: str
    name: str
    lat: float
    lon: float
    probe_host: str | None = None

    def distance_km(self, lat: float, lon: float) -> float:
        r = 6371.0
        p1, p2 = math.radians(self.lat), math.radians(lat)
        dp = p2 - p1
        dl = math.radians(lon - self.lon)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class GpuOffer:
    provider: str
    size_slug: str
    region: str
    gpu_model: str
    gpu_class: str | None
    gpu_count: int
    vram_gb: float
    vcpus: int
    ram_gb: float
    disk_gb: float
    scratch_gb: float
    price_hourly: float
    vendor: Literal["nvidia", "amd"]
    available: bool = True
    regions_known: bool = True  # False when the API listed the size with no regions (quirk)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.size_slug}:{self.region}"


@dataclass(frozen=True)
class BootImage:
    ref: str  # provider image id or slug
    kind: Literal["snapshot", "base"]


@dataclass
class InstanceSpec:
    instance_id: uuid.UUID
    name: str
    offer: GpuOffer
    image: BootImage
    bootstrap: WorkerBootstrap
    tags: list[str]
    volume_ids: list[str] = field(default_factory=list)
    worker_image: str | None = None


@dataclass
class ProviderInstance:
    provider_id: str
    name: str
    status: Literal["new", "active", "off", "archive", "unknown"]
    public_ipv4: str | None
    tags: list[str]
    created_at: datetime | None
    size_slug: str | None = None
    region: str | None = None
    port: int = 443
    # RunPod Serverless: the gateway URL that reaches the worker (instead of an IP address)
    address: str | None = None
    # what the provider is doing before the machine runs (start-up step + detail), e.g. preparing a host
    stage: str | None = None
    note: str | None = None
    # the provider reports the machine can't start (e.g. its worker keeps crashing)
    failed: str | None = None


@dataclass(frozen=True)
class ProviderTraits:
    """How a provider differs from "a cloud machine billed from create to delete"."""

    # billing starts when the machine starts working (RunPod Serverless), not when it is ordered
    bills_from_boot: bool = False
    # minimum billed time per machine; None = the runtime setting
    min_billing_s: float | None = None
    # an idle machine is kept at most this long, whatever the general idle timeout says
    max_idle_s: float | None = None
    # how long the provider may take to get a machine ready to start (not billed when bills_from_boot)
    prepare_timeout_s: float | None = None
    # firewall rules, prepared machine images and attachable model disks
    firewall: bool = True
    machine_images: bool = True
    # True: aivo reaches the worker directly over TLS pinned to the machine's own certificate.
    # False: through the provider's HTTPS gateway (RunPod Serverless).
    pinned_tls: bool = True


DEFAULT_TRAITS = ProviderTraits()
_TRAITS: dict[str, ProviderTraits] = {}


def register_traits(provider: str, traits: ProviderTraits) -> None:
    _TRAITS[provider] = traits


def traits_for(provider: str | None) -> ProviderTraits:
    if provider == "runpod" and provider not in _TRAITS:  # static parts, before the provider is created
        return ProviderTraits(
            bills_from_boot=True, min_billing_s=0, firewall=False, machine_images=False, pinned_tls=False
        )
    return _TRAITS.get(provider or "", DEFAULT_TRAITS)


@runtime_checkable
class CloudProvider(Protocol):
    name: str

    async def list_offers(self) -> list[GpuOffer]: ...

    def regions(self) -> dict[str, RegionMeta]: ...

    async def boot_image(self, offer: GpuOffer, strategy: str) -> BootImage: ...

    async def create_instance(self, spec: InstanceSpec) -> ProviderInstance: ...

    async def get_instance(self, provider_id: str) -> ProviderInstance | None: ...

    async def list_instances(self, tag: str) -> list[ProviderInstance]: ...

    async def destroy_instance(self, provider_id: str) -> None: ...

    async def ensure_network_access(
        self, tag: str, allowed_cidrs: list[str], port: int, *, ssh_cidrs: list[str] | None = None
    ) -> None: ...

    async def account_month_to_date_usd(self) -> float | None: ...

    async def gpu_quota(self) -> int | None: ...

    async def aclose(self) -> None: ...


def deployment_tag(deployment_id: uuid.UUID | str) -> str:
    return f"aivo-dep-{deployment_id}"


def instance_tag(instance_id: uuid.UUID | str) -> str:
    return f"aivo-inst-{instance_id}"


def instance_name(deployment_id: uuid.UUID | str, instance_id: uuid.UUID | str) -> str:
    return f"aivo-{str(deployment_id)[:8]}-{str(instance_id)[:8]}"


def tags_for(deployment_id: uuid.UUID | str, instance_id: uuid.UUID | str) -> list[str]:
    return ["aivo", deployment_tag(deployment_id), instance_tag(instance_id)]


def parse_size_slug(slug: str) -> dict[str, str | int] | None:
    """``gpu-h100x1-80gb`` -> {model: 'h100', count: 1, vram: 80}. Fallback when gpu_info is missing."""
    import re

    m = re.match(r"^gpu-(?P<model>[a-z0-9]+)x(?P<n>\d+)-(?P<vram>\d+)gb", slug)
    if not m:
        return None
    return {"model": m["model"], "count": int(m["n"]), "vram": int(m["vram"])}
