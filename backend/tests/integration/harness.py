"""Spin up the full app (API + controller) against a fresh DB with the simulated RunPod."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import httpx
from sqlalchemy import text

from aivo.controller.housekeeping import Housekeeping
from aivo.controller.runner import JobRunner
from aivo.main import build_context, create_app
from aivo.providers.runpod.fake import FakeRunPod

HEADERS = {"X-Requested-With": "aivo"}


class Harness:
    def __init__(self, env, *, settings: dict[str, Any] | None = None, role: str = "all", runpod: Any = None):
        self.env = env
        self.settings = {"keep_warm_s": 5, **(settings or {})}
        self.role = role
        self.runpod = runpod or FakeRunPod(
            boot_seconds=env.fake_boot_seconds, job_seconds=env.fake_job_seconds, chaos=list(env.fake_chaos)
        )

    async def __aenter__(self) -> Harness:
        from tests.conftest import reset_db

        await reset_db(self.env.database_url)
        self.ctx = build_context(self.env, runpod=self.runpod)
        async with self.ctx.sessionmaker() as s, s.begin():
            for k, v in self.settings.items():
                await s.execute(
                    text("INSERT INTO settings (key, value) VALUES (:k, CAST(:v AS jsonb))"),
                    {"k": k, "v": __import__("json").dumps(v)},
                )
        kwargs = dict(
            scheduler_interval_s=0.2,
            reconcile_interval_s=0.3,
            lease_ttl_s=6,
            runner=JobRunner(self.ctx, poll_s=0.1),
            housekeeping=Housekeeping(self.ctx, endpoint_sync_every=3),
        )
        self.app = create_app(self.env, ctx=self.ctx, role=self.role, controller_kwargs=kwargs)
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        if self.ctx.controller:
            await asyncio.wait_for(self.ctx.controller.is_leader.wait(), 10)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test", headers=HEADERS, timeout=30
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()
        with contextlib.suppress(Exception):
            await self._lifespan.__aexit__(None, None, None)
        await self.runpod.aclose()

    async def setup_admin(self) -> dict[str, Any]:
        r = await self.client.post(
            "/api/v1/auth/setup",
            json={"email": "admin@example.com", "display_name": "Admin", "password": "correct horse battery"},
        )
        assert r.status_code == 200, r.text
        return r.json()

    async def wait_job(self, job_id: str, states=("succeeded", "failed", "cancelled"), timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            r = await self.client.get(f"/api/v1/jobs/{job_id}")
            last = r.json()
            if last["state"] in states:
                return last
            await asyncio.sleep(0.1)
        raise AssertionError(f"job {job_id} stuck: {last}")

    async def wait_for(self, pred, timeout: float = 20, interval: float = 0.1):  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = await pred()
            if v:
                return v
            await asyncio.sleep(interval)
        raise AssertionError("condition not met in time")

    async def sql(self, q: str, **kw: Any) -> list[Any]:
        async with self.ctx.sessionmaker() as s:
            return list((await s.execute(text(q), kw)).all())
