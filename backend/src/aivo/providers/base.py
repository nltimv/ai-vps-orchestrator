"""Shared provider types: GPU offers, regions and the error taxonomy of the RunPod client.

* ``CapacityError``   - no GPU of this type right now -> try the next type
* ``QuotaError``      - account limit reached (workers, balance)
* ``RateLimited``     - API rate limit -> pause until ``reset_at``
* ``UnknownOutcome``  - the request may or may not have gone through
* ``TransientError``  - retry later
* ``AuthError`` / ``ConfigError`` - the operator must fix the configuration
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


class ProviderError(Exception):
    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class CapacityError(ProviderError):
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
    """One GPU type that jobs can run on (RunPod: its Serverless price per hour of worker time)."""

    provider: str
    size_slug: str  # RunPod GPU type id, e.g. "NVIDIA L40S"
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
    regions_known: bool = True

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.size_slug}:{self.region}"
