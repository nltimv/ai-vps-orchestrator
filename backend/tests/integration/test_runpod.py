"""RunPod Serverless end to end, against a local stand-in for RunPod's API and gateway (tests/integration/fake_runpod)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr

import aivo.providers.runpod.provider as rp_provider
import aivo.worker_client.client as wc
from aivo.config import EnvSettings
from aivo.controller.budget import billed_window
from aivo.db.models import Instance
from aivo.providers.runpod.provider import RunPodServerlessProvider
from aivo_protocol.worker_api import BOOTSTRAP_ENV, decode_bootstrap_env
from tests.integration.fake_runpod import FakeRunPod
from tests.integration.harness import Harness

IMG = {"kind": "image", "prompt": "a red fox in snow", "quality": "draft"}


@contextlib.asynccontextmanager
async def runpod_harness(
    env: EnvSettings, tmp_path, *, keep_warm_s: int = 3600, **fake_kw: Any
) -> AsyncIterator[tuple[Harness, FakeRunPod]]:
    async with FakeRunPod(tmp_path / "runpod", **fake_kw) as fake:
        renv = env.model_copy(
            update={
                **fake.env_overrides(),
                "runpod_api_key": SecretStr(fake.api_key),
                "runpod_keep_warm_s": keep_warm_s,
            }
        )
        provider = RunPodServerlessProvider(renv)
        async with Harness(renv, provider=provider) as h:
            yield h, fake


async def _instance(h: Harness) -> Instance:
    async with h.ctx.sessionmaker() as s:
        from sqlalchemy import select

        return (await s.execute(select(Instance).order_by(Instance.created_at))).scalars().first()  # type: ignore[return-value]


async def test_job_runs_on_a_serverless_endpoint(env, tmp_path):
    async with runpod_harness(env, tmp_path, keep_warm_s=1) as (h, fake):
        await h.setup_admin()
        est = (await h.client.post("/api/v1/estimate", json=IMG)).json()
        assert est["machine"]["gpu_class"] in ("a5000", "l4"), est

        job = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        done = await h.wait_job(job["id"], timeout=60)
        assert done["state"] == "succeeded", done
        body = (await h.client.get(done["outputs"][0]["url"])).content
        assert body[:8] == b"\x89PNG\r\n\x1a\n"

        # one load-balancing endpoint with one worker, pinned to the cheapest 24 GB card type
        assert len(fake.created) == 1
        ep = fake.created[0]
        assert ep["workers"]["max"] == 1 and ep["workers"]["min"] == 0
        assert ep["gpu"]["pools"] == ["AMPERE_24"] and len(ep["gpu"]["excludedTypes"]) == 1
        assert ep["dataCenterIds"] == ["EU-NL-1"]
        boot = decode_bootstrap_env(ep["env"][BOOTSTRAP_ENV])
        assert boot.tls_key_pem == "" and boot.listen_port == 8000 and len(boot.token_sha256) == 64

        # every request after start-up carries the worker token separately and is pinned to the worker
        (eid,) = [e for e in fake.deleted] or list(fake.endpoints)
        inst = await _instance(h)
        assert inst.endpoint_url == fake.gateway_url.format(endpoint_id=eid)
        assert inst.cert_ca_pem is None and inst.worker_ref and inst.worker_ref.startswith("wrk-")

        # billed from the worker's start, not from creating the endpoint (preparing the host is free)
        assert inst.ip_at is not None and inst.create_accepted_at is not None
        win = billed_window(inst, inst.ip_at, min_billing_s=300)
        assert win is not None and win[0] == inst.ip_at
        assert (win[1] - win[0]).total_seconds() < 300  # no minimum billing on RunPod

        # an idle worker is let go after AIVO_RUNPOD_KEEP_WARM_S: the endpoint is deleted (billing stops)
        await h.wait_for(lambda: _deleted(fake, eid), timeout=30)

        async def destroyed() -> bool:
            return await h.sql("SELECT state, outcome FROM instances") == [("destroyed", "ok")]

        await h.wait_for(destroyed, timeout=10)


async def _deleted(fake: FakeRunPod, eid: str) -> bool:
    return eid in fake.deleted


async def test_requests_are_pinned_and_use_the_aivo_token(env, tmp_path):
    async with runpod_harness(env, tmp_path) as (h, fake):
        await h.setup_admin()
        done = await h.wait_job((await h.client.post("/api/v1/jobs", json=IMG)).json()["id"], timeout=60)
        assert done["state"] == "succeeded"
        (ep,) = fake.endpoints.values()
        reqs = ep.requests
        assert all("x-aivo-token" in hdr for _, _, hdr in reqs)
        submit = [i for i, (m, p, _) in enumerate(reqs) if m == "POST" and p == "/v1/jobs"]
        assert submit, reqs
        inst = await _instance(h)
        assert inst.worker_ref == ep.worker_id


async def test_outputs_come_back_in_pieces_below_the_body_limit(env, tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "GATEWAY_MAX_BODY", 2048)
    monkeypatch.setattr(wc, "DOWNLOAD_CHUNK", 2048)
    async with runpod_harness(env, tmp_path, max_body=4096) as (h, fake):
        await h.setup_admin()
        done = await h.wait_job((await h.client.post("/api/v1/jobs", json=IMG)).json()["id"], timeout=60)
        assert done["state"] == "succeeded", done  # the checksum over the reassembled file matched
        (ep,) = fake.endpoints.values()
        size = done["outputs"][0]["bytes"] if "bytes" in done["outputs"][0] else None
        assert ep.output_range_requests > 1, (ep.output_range_requests, size)


async def test_a_vanished_worker_is_noticed_and_the_job_moves_on(env, tmp_path):
    async with runpod_harness(env, tmp_path, kill_after_submit=1) as (h, fake):
        await h.setup_admin()
        done = await h.wait_job((await h.client.post("/api/v1/jobs", json=IMG)).json()["id"], timeout=90)
        assert done["state"] == "succeeded", done
        assert len(fake.created) == 2  # a second endpoint took over
        assert len(fake.deleted) >= 1  # the first one was deleted


async def test_a_worker_that_never_starts_fails_without_billing(env, tmp_path, monkeypatch):
    monkeypatch.setattr(rp_provider, "UNHEALTHY_GIVE_UP_S", 0.5)
    async with runpod_harness(env, tmp_path) as (h, fake):
        fake.unhealthy = True
        await h.setup_admin()
        done = await h.wait_job((await h.client.post("/api/v1/jobs", json=IMG)).json()["id"], timeout=60)
        assert done["state"] == "failed"
        assert "RunPod could not start the worker software" in done["error"]
        await h.wait_for(lambda: _all_deleted(fake), timeout=20)
        inst = await _instance(h)
        assert inst.ip_at is None and billed_window(inst, inst.created_at, 300) is None


async def _all_deleted(fake: FakeRunPod) -> bool:
    return not fake.endpoints and bool(fake.deleted)


async def test_orphan_endpoints_of_this_deployment_are_deleted(env, tmp_path):
    async with runpod_harness(env, tmp_path) as (h, fake):
        await h.setup_admin()
        import httpx

        async with httpx.AsyncClient(base_url=fake.base, headers={"Authorization": f"Bearer {fake.api_key}"}) as c:
            body = {
                "name": "aivo-left-over",
                "type": "LOAD_BALANCER",
                "image": "x",
                "gpu": {"pools": ["AMPERE_24"]},
                "workers": {"min": 0, "max": 1},
                "scaling": {"type": "REQUEST_COUNT", "requestCount": 1},
                "env": {
                    "AIVO_DEPLOYMENT_ID": str(h.ctx.deployment_id),
                    "AIVO_INSTANCE_ID": "00000000-0000-0000-0000-000000000000",
                },
            }
            mine = (await c.post("/v2/serverless", json=body)).json()["id"]
            other = (
                await c.post("/v2/serverless", json={**body, "name": "someone-else", "env": {"MODEL": "x"}})
            ).json()["id"]
        await h.wait_for(lambda: _deleted(fake, mine), timeout=20)
        assert other in fake.endpoints


@pytest.mark.parametrize("limit", [0])
async def test_the_account_worker_limit_is_reported(env, tmp_path, limit):
    async with runpod_harness(env, tmp_path, max_workers=limit) as (h, fake):
        await h.setup_admin()
        job = (await h.client.post("/api/v1/jobs", json=IMG)).json()

        async def waiting() -> Any:
            j = (await h.client.get(f"/api/v1/jobs/{job['id']}")).json()
            return j if j["state"] in ("waiting_capacity", "failed") else None

        j = await h.wait_for(waiting, timeout=30)
        assert "limit" in (j.get("error") or "").lower(), j
        attempts = await h.sql("SELECT outcome FROM placement_attempts")
        assert ("quota",) in attempts
