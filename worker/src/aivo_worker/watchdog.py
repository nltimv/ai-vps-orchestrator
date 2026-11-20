"""Opt-in self-destruct: if the backend stops talking to us (e.g. a sleeping
laptop), delete this machine so it cannot keep billing.

Uses a provider token scoped to deleting machines; recommended only within a
dedicated cloud team/account.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from aivo_protocol.worker_api import WatchdogConfig
from aivo_worker.security import TokenAuth

log = logging.getLogger(__name__)

METADATA_ID_URL = "http://169.254.169.254/metadata/v1/id"


async def digitalocean_self_destruct(token: str) -> None:
    async with httpx.AsyncClient(timeout=15) as c:
        droplet_id = (await c.get(METADATA_ID_URL)).text.strip()
        r = await c.delete(
            f"https://api.digitalocean.com/v2/droplets/{droplet_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code not in (204, 404):
            raise RuntimeError(f"self-destruct failed: HTTP {r.status_code}")


class Watchdog:
    def __init__(
        self,
        cfg: WatchdogConfig,
        auth: TokenAuth,
        destroy: Callable[[], Awaitable[None]] | None = None,
        check_every_s: float = 30.0,
    ):
        self.cfg = cfg
        self.auth = auth
        self.check_every_s = check_every_s
        self.fired = False
        if destroy is None:
            if not cfg.token:
                raise ValueError("watchdog enabled without a token")
            token = cfg.token
            destroy = lambda: digitalocean_self_destruct(token)  # noqa: E731
        self.destroy = destroy

    def should_fire(self, now_mono: float | None = None, now_wall: float | None = None) -> str | None:
        now_mono = time.monotonic() if now_mono is None else now_mono
        now_wall = time.time() if now_wall is None else now_wall
        if now_mono - self.auth.last_contact > self.cfg.max_silence_s:
            return "backend silent"
        if self.cfg.deadline_unix and now_wall > self.cfg.deadline_unix:
            return "absolute deadline reached"
        return None

    async def run(self) -> None:
        while not self.fired:
            await asyncio.sleep(self.check_every_s)
            reason = self.should_fire()
            if reason:
                log.error("watchdog firing: %s", reason)
                for attempt in range(5):
                    try:
                        await self.destroy()
                        self.fired = True
                        return
                    except Exception as e:
                        log.error("self-destruct attempt %d failed: %s", attempt + 1, e)
                        await asyncio.sleep(10 * (attempt + 1))
