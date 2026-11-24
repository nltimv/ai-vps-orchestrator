"""Thin RunPod REST API v2 client (https://api.runpod.io/v2): pagination, error classification."""

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
)

log = logging.getLogger(__name__)

QUOTA_HINTS = ("max worker", "worker limit", "workers limit", "quota", "limit")
FUNDS_HINTS = ("balance", "credit", "insufficient funds", "funds")
CAPACITY_HINTS = ("no gpu", "not available", "unavailable", "capacity", "sold out", "no available")


def _message(body: Any) -> str:
    if isinstance(body, dict):
        parts = [str(body.get("detail") or body.get("title") or body.get("error") or body.get("message") or "")]
        errs = body.get("errors")
        if isinstance(errs, list) and errs:
            parts.append("; ".join(str(e) for e in errs[:3]))
        return " - ".join(p for p in parts if p)
    return str(body or "")[:300]


def classify(status: int, body: Any, *, creating: bool = False) -> ProviderError:
    msg = _message(body)
    low = msg.lower()
    text = f"HTTP {status}: {msg}".strip()
    if status in (401, 403):
        return AuthError(f"{text} (check AIVO_RUNPOD_API_KEY and its permissions)", http_status=status)
    if status == 429:
        return RateLimited(text, http_status=status)
    if status >= 500:
        return UnknownOutcome(text, http_status=status) if creating else TransientError(text, http_status=status)
    if status in (400, 402, 409, 422):
        if any(h in low for h in FUNDS_HINTS):
            return QuotaError(f"{text} (top up your RunPod balance)", http_status=status)
        if any(h in low for h in QUOTA_HINTS) and "rate" not in low:
            return QuotaError(f"{text} (RunPod's limit on workers across your endpoints)", http_status=status)
        if any(h in low for h in CAPACITY_HINTS):
            return CapacityError(text, http_status=status)
        if "image" in low:
            return ConfigError(f"{text} (check AIVO_WORKER_IMAGE)", http_status=status)
        return ConfigError(text, http_status=status)
    if status == 404:
        return ConfigError(text, http_status=status)
    return ProviderError(text, http_status=status)


class RunPodClient:
    def __init__(
        self,
        api_key: str,
        base: str = "https://api.runpod.io",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base = base.rstrip("/")
        self._c = httpx.AsyncClient(
            base_url=self.base,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(30, connect=10),
            transport=transport,
        )
        self.paused_until = 0.0

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
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        """JSON body of a successful response ({} when empty); None for 404 when ``missing_ok``."""
        for attempt in range(4):
            wait = self.paused_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(min(wait, 60))
            try:
                r = await self._c.request(method, path, json=json, params=params)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if creating:
                    raise UnknownOutcome(f"{method} {path}: {e}") from e
                if attempt == 3:
                    raise TransientError(f"{method} {path}: {e}") from e
                await asyncio.sleep(2**attempt)
                continue
            if r.status_code < 300:
                if r.status_code == 204 or not r.content:
                    return {}
                return r.json()  # type: ignore[no-any-return]
            if r.status_code == 404 and missing_ok:
                return None
            body: Any = None
            try:
                body = r.json()
            except ValueError:
                body = r.text
            err = classify(r.status_code, body, creating=creating and method == "POST")
            if isinstance(err, RateLimited):
                retry_after = r.headers.get("retry-after", "")
                secs = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 30.0
                err.reset_at = time.time() + secs
                self.paused_until = time.monotonic() + secs
                if not creating and attempt < 3:
                    continue
            if isinstance(err, TransientError) and attempt < 3:
                await asyncio.sleep(2**attempt)
                continue
            raise err
        raise TransientError(f"{method} {path}: retries exhausted")

    async def paginate(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):
            p = dict(params or {})
            if cursor:
                p["cursor"] = cursor
            data = await self.request("GET", path, params=p) or {}
            out.extend(data.get(key) or [])
            page = data.get("pagination") or {}
            cursor = page.get("nextCursor")
            if not page.get("hasNextPage") or not cursor:
                break
        return out
