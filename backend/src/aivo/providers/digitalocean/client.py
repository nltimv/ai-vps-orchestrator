"""Thin DigitalOcean API v2 client: rate limiting, pagination, error classification."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from aivo.providers.base import (
    AuthError,
    CapacityError,
    ConfigError,
    ProviderError,
    QuotaError,
    RateLimited,
    TransientError,
    UnknownOutcome,
    VolumeBusy,
)

log = logging.getLogger(__name__)

CAPACITY_HINTS = (
    "not available",
    "unavailable",
    "capacity",
    "out of stock",
    "sold out",
    "insufficient",
    "no available",
    "currently not",
    "try again later",
    "not enough",
    "cannot be created in",
)
QUOTA_HINTS = ("limit", "exceed", "quota")
VOLUME_HINTS = ("volume",)
VOLUME_BUSY_HINTS = ("attached", "in use", "already", "busy")


def classify(status: int, body: dict[str, Any] | None, *, creating: bool = False) -> ProviderError:
    msg = ""
    if body:
        msg = str(body.get("message") or body.get("id") or "")
    low = msg.lower()
    text = f"HTTP {status}: {msg}".strip()
    if status in (401, 403):
        return AuthError(text, http_status=status)
    if status == 429:
        return RateLimited(text, http_status=status)
    if status >= 500:
        return UnknownOutcome(text, http_status=status) if creating else TransientError(text, http_status=status)
    if status == 422 or status == 400:
        if any(h in low for h in VOLUME_HINTS) and any(h in low for h in VOLUME_BUSY_HINTS):
            return VolumeBusy(text, http_status=status)
        if "image" in low:  # e.g. an unknown base image slug: retrying elsewhere won't help
            return ConfigError(f"{text} (check AIVO_DO_BASE_IMAGE)", http_status=status)
        if (
            "droplet limit" in low
            or ("gpu" in low and "limit" in low)
            or (any(h in low for h in QUOTA_HINTS) and "rate" not in low and "size" not in low)
        ):
            return QuotaError(text, http_status=status)
        if any(h in low for h in CAPACITY_HINTS) or "size" in low or "region" in low:
            return CapacityError(text, http_status=status)
        return ConfigError(text, http_status=status)
    if status == 404:
        return ConfigError(text, http_status=status)
    return ProviderError(text, http_status=status)


class TokenBucket:
    def __init__(self, per_minute: int = 200):
        self.capacity = per_minute
        self.tokens = float(per_minute)
        self.rate = per_minute / 60.0
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()
        self.paused_until = 0.0

    async def take(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                if now < self.paused_until:
                    await asyncio.sleep(self.paused_until - now)
                    continue
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class DOClient:
    def __init__(
        self,
        token: str,
        base: str = "https://api.digitalocean.com",
        *,
        per_minute: int = 200,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base = base.rstrip("/")
        self.bucket = TokenBucket(per_minute)
        self._c = httpx.AsyncClient(
            base_url=self.base,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(30, connect=10),
            transport=transport,
        )
        self.remaining: int | None = None

    async def aclose(self) -> None:
        await self._c.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        creating: bool = False,
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> dict[str, Any]:
        for attempt in range(4):
            await self.bucket.take()
            try:
                r = await self._c.request(method, path, json=json, params=params)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if creating:
                    raise UnknownOutcome(f"{method} {path}: {e}") from e
                if attempt == 3:
                    raise TransientError(f"{method} {path}: {e}") from e
                await asyncio.sleep(2**attempt)
                continue
            self._note_limits(r)
            if r.status_code in ok:
                if r.status_code == 204 or not r.content:
                    return {}
                return r.json()  # type: ignore[no-any-return]
            body = None
            try:
                body = r.json()
            except ValueError:
                pass
            err = classify(r.status_code, body, creating=creating and method == "POST")
            if isinstance(err, RateLimited):
                reset = float(r.headers.get("ratelimit-reset", time.time() + 30))
                err.reset_at = reset
                self.bucket.paused_until = time.monotonic() + max(1.0, reset - time.time())
                if not creating and attempt < 3:
                    continue
            if isinstance(err, TransientError) and attempt < 3:
                await asyncio.sleep(2**attempt)
                continue
            raise err
        raise TransientError(f"{method} {path}: retries exhausted")

    def _note_limits(self, r: httpx.Response) -> None:
        rem = r.headers.get("ratelimit-remaining")
        if rem and rem.isdigit():
            self.remaining = int(rem)
            if self.remaining < 100:
                # slow down well before the hourly limit
                self.bucket.rate = max(0.2, self.bucket.rate / 2)

    async def paginate(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self.request("GET", path, params={**(params or {}), "per_page": 200, "page": page})
            out.extend(data.get(key, []))
            if not data.get("links", {}).get("pages", {}).get("next"):
                return out
            page += 1
            if page > 50:
                return out
