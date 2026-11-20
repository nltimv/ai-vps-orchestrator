"""Failure injection against the full stack (fake provider chaos)."""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from aivo.controller.budget import admit
from aivo.controller.lifecycle import INSTANCE_TRANSITIONS, IllegalTransition, transition_instance
from aivo.providers.base import deployment_tag, instance_tag
from tests.integration.harness import Harness

IMG = {"kind": "image", "prompt": "a lighthouse", "quality": "draft", "model_id": "z-image-turbo"}


async def test_capacity_errors_fall_back_to_next_candidate(env):
    async with Harness(env) as h:
        await h.setup_admin()
        est = (await h.client.post("/api/v1/estimate", json=IMG)).json()
        order = [est["machine"]["key"]] + [a["key"] for a in est["alternatives"]]
        top2 = order[:2]
        for key in top2:
            _, size, region = key.split(":")
            h.provider.chaos[f"{size}:{region}"] = ["capacity"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        done = await h.wait_job(j["id"])
        assert done["state"] == "succeeded", done
        rows = await h.sql("SELECT size_slug, region, outcome, rank FROM placement_attempts ORDER BY id")
        outcomes = [(r[2], r[3]) for r in rows]
        assert outcomes[:2] == [("capacity", 0), ("capacity", 1)] and outcomes[2] == ("created", 2)
        inst = await h.sql("SELECT offer_key FROM instances")
        assert inst[0][0] == order[2]
        # next estimate avoids the sold-out offers (cooldown)
        est2 = (await h.client.post("/api/v1/estimate", json=IMG)).json()
        keys = {est2["machine"]["key"]} | {a["key"] for a in est2["alternatives"]}
        assert not (set(top2) & keys)


async def test_unknown_outcome_adopts_exactly_one_machine(env):
    async with Harness(env) as h:
        await h.setup_admin()
        h.provider.chaos["*"] = ["unknown_created"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        assert (await h.wait_job(j["id"]))["state"] == "succeeded"
        assert len(h.provider.machines) == 1
        rows = await h.sql("SELECT outcome FROM placement_attempts ORDER BY id")
        assert "unknown_adopted" in [r[0] for r in rows]


async def test_unknown_outcome_lost_retries(env):
    async with Harness(env) as h:
        await h.setup_admin()
        h.provider.chaos["*"] = ["unknown_lost"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        assert (await h.wait_job(j["id"]))["state"] == "succeeded"
        assert len(h.provider.machines) == 1


async def test_no_capacity_anywhere_waits_then_fails(env):
    async with Harness(env, settings={"capacity_wait_max_s": 1}) as h:
        await h.setup_admin()
        h.provider.chaos["*"] = ["capacity"] * 50
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        done = await h.wait_job(j["id"], timeout=30)
        assert done["state"] == "failed"
        assert "available" in done["error"]
        assert h.provider.machines == {}


async def test_orphans_destroyed_only_for_this_deployment(env):
    async with Harness(env) as h:
        await h.setup_admin()
        mine = h.provider.inject_orphan(["aivo", deployment_tag(h.ctx.deployment_id), instance_tag(uuid.uuid4())])
        other = h.provider.inject_orphan(["aivo", deployment_tag(uuid.uuid4()), instance_tag(uuid.uuid4())])
        await h.wait_for(lambda: _gone(h, mine), timeout=10)
        assert other in h.provider.machines


async def _gone(h, pid):
    return pid not in h.provider.machines


async def test_vanished_machine_requeues_job(env):
    async with Harness(env) as h:
        await h.setup_admin()
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        assert (await h.wait_job(j["id"]))["state"] == "succeeded"
        pid = next(iter(h.provider.machines))
        h.provider.vanish(pid)

        async def destroyed():
            rows = await h.sql("SELECT state, outcome FROM instances")
            return rows[0][0] == "destroyed" and rows[0][1] == "external"

        await h.wait_for(destroyed, timeout=10)
        j2 = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        assert (await h.wait_job(j2["id"]))["state"] == "succeeded"


async def test_out_of_memory_retries_on_bigger_gpu(env):
    async with Harness(env) as h:
        await h.setup_admin()
        h.provider.chaos["gpu-4000adax1-20gb:tor1"] = ["engine_oom"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        done = await h.wait_job(j["id"], timeout=40)
        assert done["state"] == "succeeded", done
        rows = await h.sql("SELECT min_vram_gb, attempt FROM jobs")
        assert rows[0][0] > 20 and rows[0][1] == 1
        sizes = {r[0] for r in await h.sql("SELECT size_slug FROM instances WHERE state <> 'destroyed'")}
        assert "gpu-4000adax1-20gb" not in sizes


async def test_engine_failure_is_friendly(env):
    async with Harness(env) as h:
        await h.setup_admin()
        h.provider.chaos["*"] = ["engine_fail"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        done = await h.wait_job(j["id"])
        assert done["state"] == "failed"
        assert done["error"] == "Something went wrong while creating this."
        r = await h.client.post(f"/api/v1/jobs/{j['id']}/retry")
        assert r.status_code == 201
        assert (await h.wait_job(j["id"]))["state"] == "succeeded"


async def test_cancel_while_waiting_and_running(env):
    async with Harness(env) as h:
        await h.setup_admin()
        h.provider.boot_seconds = 3
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        await asyncio.sleep(0.3)
        r = await h.client.post(f"/api/v1/jobs/{j['id']}/cancel")
        assert r.json()["state"] == "cancelled"
        h.provider.job_seconds = 5
        j2 = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        await h.wait_job(j2["id"], states=("running",), timeout=20)
        await h.client.post(f"/api/v1/jobs/{j2['id']}/cancel")
        done = await h.wait_job(j2["id"], timeout=20)
        assert done["state"] == "cancelled"


async def test_budget_hard_cap_blocks_and_drains(env):
    async with Harness(env, settings={"hard_cap_usd": 0.02, "soft_cap_usd": 0.01}) as h:
        await h.setup_admin()
        r = await h.client.post("/api/v1/jobs", json=IMG)
        assert r.status_code == 201
        done = await h.wait_job(r.json()["id"])
        # a reservation for any GPU exceeds $0.02 -> fails with a budget message
        assert done["state"] == "failed" and "spending limit" in done["error"]
        assert h.provider.machines == {}


async def test_soft_cap_requires_confirmation(env):
    async with Harness(env, settings={"soft_cap_usd": 0.0, "hard_cap_usd": 100}) as h:
        await h.setup_admin()
        r = await h.client.post("/api/v1/jobs", json=IMG)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "confirm_soft_cap"
        r = await h.client.post("/api/v1/jobs", json={**IMG, "confirm_over_soft_cap": True})
        assert r.status_code == 201


async def test_concurrent_admission_respects_cap(env):
    async with Harness(env, settings={"hard_cap_usd": 1.0}) as h:
        rs = await h.ctx.runtime(fresh=True)
        amount = Decimal("0.4")

        async def try_admit():
            async with h.ctx.sessionmaker() as s, s.begin():
                from aivo.controller.common import utcnow

                adm = await admit(s, rs, utcnow(), amount)
                if adm.ok:
                    await s.execute(
                        text(
                            "INSERT INTO instances (id, name, provider, offer_key, size_slug, region, gpu_class, gpu_model,"
                            " vendor, vram_gb, ram_gb, price_hourly, state, state_version, port, reserved_usd, manual,"
                            " files_present, models_loaded, candidates, attempt_index, created_at, state_changed_at)"
                            " VALUES (:id, :n, 'fake', 'k', 's', 'tor1', 'l40s', 'L40S', 'nvidia', 48, 64, 1.57,"
                            " 'pending', 0, 443, :r, false, '[]', '[]', '[]', 0, now(), now())"
                        ),
                        {"id": uuid.uuid4(), "n": f"t-{uuid.uuid4().hex[:8]}", "r": amount},
                    )
                return adm.ok

        results = await asyncio.gather(*(try_admit() for _ in range(20)))
        assert sum(results) == 2


async def test_restart_mid_job_resumes_without_duplicates(env):
    async with Harness(env) as h:
        await h.setup_admin()
        h.provider.job_seconds = 2
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        await h.wait_job(j["id"], states=("running",), timeout=20)
        # simulate a controller crash + restart
        ctrl = h.ctx.controller
        await ctrl.stop()
        await ctrl.lease.release()
        ctrl._stopping = False
        await ctrl.start()
        done = await h.wait_job(j["id"], timeout=30)
        assert done["state"] == "succeeded", done
        assert len(done["outputs"]) == 1
        assert (await h.sql("SELECT count(*) FROM instances"))[0][0] == 1
        assert (await h.sql("SELECT count(*) FROM assets WHERE role='output'"))[0][0] == 1


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


def test_transition_table_is_closed():
    states = set(INSTANCE_TRANSITIONS)
    for frm, tos in INSTANCE_TRANSITIONS.items():
        assert tos <= states
    assert INSTANCE_TRANSITIONS["destroyed"] == set()
    # every live state can reach destroying/destroyed
    for s in states - {"destroyed", "destroying"}:
        assert {"destroying", "destroyed"} & INSTANCE_TRANSITIONS[s]


async def test_illegal_transition_raises(env):
    async with Harness(env, role="api") as h:
        async with h.ctx.sessionmaker() as s:
            with pytest.raises(IllegalTransition):
                await transition_instance(s, uuid.uuid4(), "destroyed", "ready")
