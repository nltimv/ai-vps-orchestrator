"""Failure injection against the full stack (simulated RunPod chaos)."""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from aivo.controller.budget import admit
from aivo.controller.lifecycle import IllegalTransition, transition_job
from tests.integration.harness import Harness

IMG = {"kind": "image", "prompt": "a lighthouse", "quality": "draft", "model_id": "z-image-turbo"}


async def test_no_worker_in_time_moves_to_the_next_gpu_type(env):
    async with Harness(env, settings={"gpu_fallback_after_s": 1}) as h:
        await h.setup_admin()
        h.runpod.chaos = ["no_gpu"]  # the first GPU type's endpoint never gets a worker
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        done = await h.wait_job(j["id"], timeout=30)
        assert done["state"] == "succeeded", done
        eps = list(h.runpod.endpoints.values())
        assert len(eps) == 2
        assert [x.status for x in eps[0].jobs] == ["CANCELLED"]  # taken back from the first queue
        attempts = await h.sql("SELECT size_slug, outcome FROM placement_attempts ORDER BY at")
        assert attempts[1][1] == "capacity" and attempts[0][0] == attempts[1][0] != attempts[-1][0]


async def test_no_gpu_anywhere_waits_then_fails(env):
    async with Harness(env, settings={"gpu_fallback_after_s": 1, "capacity_wait_max_s": 4}) as h:
        await h.setup_admin()
        h.runpod.chaos = ["no_gpu"] * 20
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        done = await h.wait_job(j["id"], timeout=40)
        assert done["state"] == "failed" and "No GPU" in done["error"], done
        assert (await h.sql("SELECT count(*) FROM instances"))[0][0] == 0  # nothing was billed


async def test_crashed_worker_is_replaced_and_the_job_retried(env):
    async with Harness(env) as h:
        await h.setup_admin()
        h.runpod.chaos = ["worker_crash"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        done = await h.wait_job(j["id"], timeout=40)
        assert done["state"] == "succeeded", done
        assert len(done["outputs"]) == 1
        workers = (await h.sql("SELECT count(DISTINCT provider_instance_id) FROM instances"))[0][0]
        assert workers == 2  # RunPod gave the job to a second worker
        assert (await h.sql("SELECT count(*) FROM assets WHERE role='output'"))[0][0] == 1


async def test_out_of_memory_retries_on_a_bigger_gpu(env):
    async with Harness(env) as h:
        await h.setup_admin()
        h.runpod.chaos = ["oom"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        done = await h.wait_job(j["id"], timeout=40)
        assert done["state"] == "succeeded", done
        rows = await h.sql("SELECT gpu_type, min_vram_gb FROM jobs")
        assert rows[0][1] is not None and rows[0][1] > 20  # bumped past the first GPU's memory
        assert len(h.runpod.endpoints) == 2


async def test_engine_failure_is_friendly(env):
    async with Harness(env) as h:
        await h.setup_admin()
        h.runpod.chaos = ["engine_fail"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        done = await h.wait_job(j["id"], timeout=30)
        assert done["state"] == "failed"
        assert done["error"] == "Something went wrong while creating this."
        # the worker's log lines are kept for admins
        log = (await h.client.get(f"/api/v1/jobs/{j['id']}/log")).json()
        assert any("engine" in ln["text"] for ln in log["lines"])
        assert (await h.sql("SELECT count(*) FROM assets WHERE role='output'"))[0][0] == 0


async def test_cancel_while_waiting_for_a_gpu_and_while_running(env):
    async with Harness(env, settings={"gpu_fallback_after_s": 600}) as h:
        await h.setup_admin()
        h.runpod.chaos = ["no_gpu"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        await h.wait_job(j["id"], states=("assigned",), timeout=10)
        await h.client.post(f"/api/v1/jobs/{j['id']}/cancel")
        assert (await h.wait_job(j["id"], timeout=20))["state"] == "cancelled"
        (first,) = h.runpod.endpoints.values()
        await h.wait_for(lambda: _status_is(first.jobs[0], "CANCELLED"), timeout=10)

        first.no_gpu = False  # GPUs of this type are back
        h.runpod.job_seconds = 5
        j2 = (
            await h.client.post("/api/v1/jobs", json={**IMG, "model_id": "z-image-turbo", "quality": "standard"})
        ).json()
        await h.wait_job(j2["id"], states=("running",), timeout=30)
        await h.client.post(f"/api/v1/jobs/{j2['id']}/cancel")
        done = await h.wait_job(j2["id"], timeout=30)
        assert done["state"] == "cancelled"
        assert (await h.sql("SELECT count(*) FROM assets WHERE role='output'"))[0][0] == 0


async def _status_is(job, status):  # type: ignore[no-untyped-def]
    return job.status == status


async def test_tickets_only_open_their_own_job(env):
    async with Harness(env) as h:
        await h.setup_admin()
        h.runpod.job_seconds = 4
        a = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        b = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        await h.wait_job(a["id"], states=("running",), timeout=30)
        await h.wait_job(b["id"], states=("running", "preparing", "assigned"), timeout=30)
        tokens = {j.input["job_id"]: j.input["token"] for ep in h.runpod.endpoints.values() for j in ep.jobs}
        body = {"stage": "generating", "progress": 0.1}
        url = f"/api/v1/worker/jobs/{b['id']}/progress"
        assert (
            await h.client.post(url, json=body, headers={"Authorization": f"Bearer {tokens[a['id']]}"})
        ).status_code == 401
        assert (await h.client.post(url, json=body, headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await h.client.post(url, json=body)).status_code == 401
        r = await h.client.get(
            f"/api/v1/worker/jobs/{uuid.uuid4()}/inputs/x", headers={"Authorization": f"Bearer {tokens[a['id']]}"}
        )
        assert r.status_code == 401
        # a worker can't deliver a file that doesn't match what it says it sent
        r = await h.client.put(
            f"/api/v1/worker/jobs/{a['id']}/outputs/x.png",
            params={"media_type": "image/png", "sha256": "0" * 64},
            content=b"not that",
            headers={"Authorization": f"Bearer {tokens[a['id']]}"},
        )
        assert r.status_code == 422
        for jid in (a["id"], b["id"]):
            assert (await h.wait_job(jid, timeout=30))["state"] == "succeeded"


async def test_orphan_endpoints_deleted_only_for_this_deployment(env):
    async with Harness(env) as h:
        await h.setup_admin()
        body = {
            "name": "left-over",
            "type": "QUEUE",
            "image": "x",
            "gpu": {"pools": ["AMPERE_24"]},
            "workers": {"min": 0, "max": 1},
            "scaling": {"type": "QUEUE_DELAY", "queueDelay": 1},
        }
        mine = (await h.runpod.create_endpoint({**body, "env": {"AIVO_DEPLOYMENT_ID": str(h.ctx.deployment_id)}}))["id"]
        other = (await h.runpod.create_endpoint({**body, "env": {"AIVO_DEPLOYMENT_ID": str(uuid.uuid4())}}))["id"]

        async def deleted():
            return mine in h.runpod.deleted

        await h.wait_for(deleted, timeout=15)
        assert other in h.runpod.endpoints


async def test_budget_hard_cap_blocks(env):
    async with Harness(env, settings={"hard_cap_usd": 0.0002, "soft_cap_usd": 0.0001}) as h:
        await h.setup_admin()
        r = await h.client.post("/api/v1/jobs", json={**IMG, "confirm_over_soft_cap": True})
        assert r.status_code == 201
        done = await h.wait_job(r.json()["id"])
        # a reservation for any GPU exceeds the cap -> fails with a budget message, nothing sent to RunPod
        assert done["state"] == "failed" and "spending limit" in done["error"]
        assert h.runpod.endpoints == {}


async def test_soft_cap_requires_confirmation(env):
    async with Harness(env, settings={"soft_cap_usd": 0.0, "hard_cap_usd": 100}) as h:
        await h.setup_admin()
        r = await h.client.post("/api/v1/jobs", json=IMG)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "confirm_soft_cap"
        r = await h.client.post("/api/v1/jobs", json={**IMG, "confirm_over_soft_cap": True})
        assert r.status_code == 201


async def test_concurrent_admission_respects_cap(env):
    async with Harness(env, settings={"hard_cap_usd": 1.0}, role="api") as h:  # no controller touching the jobs
        await h.setup_admin()
        user_id = (await h.sql("SELECT id FROM users"))[0][0]
        rs = await h.ctx.runtime(fresh=True)
        amount = Decimal("0.4")

        async def try_admit():
            async with h.ctx.sessionmaker() as s, s.begin():
                from aivo.controller.common import utcnow

                adm = await admit(s, rs, utcnow(), amount)
                if adm.ok:
                    await s.execute(
                        text(
                            "INSERT INTO jobs (id, user_id, kind, mode, model_id, friendly_params, resolved,"
                            " input_asset_ids, preference, placement_mode, allow_fallback, state, attempt, max_attempts,"
                            " progress, stage, cancel_requested, candidates, reserved_usd, log, created_at)"
                            " VALUES (:id, :u, 'image', 't2i', 'z-image-turbo', '{}', '{}', '{}', 'balanced', 'auto',"
                            " true, 'assigned', 0, 2, 0, 'x', false, '[]', :r, '[]', now())"
                        ),
                        {"id": uuid.uuid4(), "u": user_id, "r": amount},
                    )
                return adm.ok

        results = await asyncio.gather(*(try_admit() for _ in range(20)))
        assert sum(results) == 2


async def test_restart_mid_job_resumes_without_duplicates(env):
    async with Harness(env) as h:
        await h.setup_admin()
        h.runpod.job_seconds = 2
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        await h.wait_job(j["id"], states=("running",), timeout=20)
        # a controller crash + restart: the worker carries on and delivers to the API
        ctrl = h.ctx.controller
        await ctrl.stop()
        await ctrl.lease.release()
        ctrl._stopping = False
        await ctrl.start()
        done = await h.wait_job(j["id"], timeout=30)
        assert done["state"] == "succeeded", done
        assert len(done["outputs"]) == 1
        assert (await h.sql("SELECT count(*) FROM assets WHERE role='output'"))[0][0] == 1
        assert sum(len(ep.jobs) for ep in h.runpod.endpoints.values()) == 1  # submitted once


async def test_lease_fencing(env):
    async with Harness(env, role="api") as h:
        from aivo.controller.lease import LeaseManager

        a = LeaseManager(h.ctx.sessionmaker, ttl_s=1)
        b = LeaseManager(h.ctx.sessionmaker, ttl_s=1)
        assert await a.try_acquire()
        assert not await b.try_acquire()
        await asyncio.sleep(1.2)
        assert await b.try_acquire()  # a expired
        assert not await a.renew()  # a is fenced out
        async with h.ctx.sessionmaker() as s:
            assert await b.still_valid(s)
            assert not await a.still_valid(s)


async def test_illegal_transition_raises(env):
    async with Harness(env, role="api") as h:
        async with h.ctx.sessionmaker() as s:
            with pytest.raises(IllegalTransition):
                await transition_job(s, uuid.uuid4(), "succeeded", "running")


async def test_estimate_when_no_gpu_fits_says_so(env):
    """Regression: this path read a database row after rolling back and crashed with a 500."""
    async with Harness(env, settings={"max_price_hourly": 0.01}) as h:
        await h.setup_admin()
        r = await h.client.post("/api/v1/estimate", json=IMG)
        assert r.status_code == 200, r.text
        assert r.json()["machine"] is None and "available" in r.json()["message"]
