from aivo.controller.bake import Baker
from tests.integration.harness import Harness

IMG = {"kind": "image", "prompt": "a lighthouse", "quality": "draft", "model_id": "z-image-turbo"}


async def test_bake_creates_snapshot_and_new_machines_boot_from_it(env):
    async with Harness(env, settings={"cache_policy": {"snapshot_regions": ["tor1", "nyc2"]}}) as h:
        await h.setup_admin()
        b = Baker(h.ctx, poll_s=0.05)
        run_id = await b.start()
        await b.run(run_id)
        rows = await h.sql("SELECT state, step, data->>'stage' FROM background_runs")
        assert rows[0][0] == "succeeded", rows
        assert rows[0][2] in ("checking_engine", "cleaning_up")  # steps were read from the machine
        snaps = await h.sql(
            "SELECT region, status, min_disk_gb FROM cache_resources WHERE kind='snapshot' ORDER BY region"
        )
        assert [(r[0], r[1]) for r in snaps] == [("nyc2", "ready"), ("tor1", "ready")]
        assert h.provider.transfers and h.provider.transfers[0][1] == "nyc2"
        # bake machine is gone
        assert not any(m.pi.name.startswith("aivo-bake") for m in h.provider.machines.values())
        # next machine uses the prepared image and the estimate knows it
        est = (await h.client.post("/api/v1/estimate", json=IMG)).json()
        assert "Machine image prepared here — quicker start" in est["machine"]["reasons"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        assert (await h.wait_job(j["id"]))["state"] == "succeeded"
        kinds = await h.sql("SELECT image_kind FROM instances")
        assert kinds[0][0] == "snapshot"
        # re-bake deprecates the old image and garbage-collects it
        run2 = await b.start()
        await b.run(run2)
        assert h.provider.deleted_images


async def test_slow_bake_is_not_mistaken_for_an_orphan(env):
    """A real bake takes 10+ minutes. Its machine carries the deployment tag but isn't a tracked instance,
    so the reconciler's orphan cleanup must leave it alone while the bake runs."""
    async with Harness(env) as h:
        await h.setup_admin()
        h.provider.bake_seconds = 3  # well past the harness's 1 s orphan age, several reconcile ticks
        b = Baker(h.ctx, poll_s=0.1)
        run_id = await b.start()
        await b.run(run_id)
        rows = await h.sql("SELECT state, step FROM background_runs")
        assert rows[0][0] == "succeeded", rows
        orphans = await h.sql("SELECT count(*) FROM events WHERE type = 'orphan.destroyed'")
        assert orphans[0][0] == 0


async def test_bake_failure_on_the_machine_ends_the_bake_at_once(env):
    """The bake machine reports a failed step: no waiting for a power-off that never comes."""
    import time

    async with Harness(env) as h:
        await h.setup_admin()
        h.provider.bake_seconds = 1
        h.provider.bake_chaos = "engine_check_fails"
        b = Baker(h.ctx, poll_s=0.1)
        run_id = await b.start()
        t0 = time.monotonic()
        await b.run(run_id)
        assert time.monotonic() - t0 < 10
        state, data = (await h.sql("SELECT state, data FROM background_runs"))[0]
        assert state == "failed" and "can't use the GPU" in data["error"]
        assert any("PyTorch cannot see the GPU" in ln for ln in data["log_tail"])
        assert not any(m.pi.name.startswith("aivo-bake") for m in h.provider.machines.values())  # destroyed
        run = (await h.client.get("/api/v1/admin/cache")).json()["runs"][0]
        assert run["state"] == "failed" and "can't use the GPU" in run["error"] and run["log_tail"]


async def test_bake_machine_left_behind_is_still_cleaned_up(env):
    """If the bake run is over (or the backend lost track of it), its machine is an orphan again."""
    from aivo.providers.base import deployment_tag

    async with Harness(env) as h:
        await h.setup_admin()
        pid = h.provider.inject_orphan(["aivo", deployment_tag(h.ctx.deployment_id), "aivo-bake"])

        async def gone() -> bool:
            return pid not in h.provider.machines

        await h.wait_for(gone, timeout=10)


async def test_admin_cache_status(env):
    async with Harness(env) as h:
        await h.setup_admin()
        st = (await h.client.get("/api/v1/admin/cache")).json()
        assert st["bake_supported"] is True and st["mirror"]["configured"] is False


async def test_data_residency(env):
    """Generated content persists only on the backend's encrypted media volume."""
    async with Harness(env) as h:
        await h.setup_admin()
        j = (await h.client.post("/api/v1/jobs", json={**IMG, "prompt": "secret-prompt-xyz"})).json()
        assert (await h.wait_job(j["id"]))["state"] == "succeeded"
        m = next(iter(h.provider.machines.values()))
        # nothing left in the worker's (tmpfs) job dir
        leftovers = [p for p in (m.root / "jobs").rglob("*") if p.is_file()]
        assert leftovers == []
        # nothing on the worker's model disk besides (simulated) weights
        assert not any(p.suffix in (".png", ".mp4", ".m4a") for p in m.root.rglob("*"))
        # the bootstrap that went to the provider never contains the prompt
        assert "secret-prompt-xyz" not in m.spec.bootstrap.model_dump_json()
