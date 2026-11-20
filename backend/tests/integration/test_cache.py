from aivo.controller.bake import Baker
from tests.integration.harness import Harness

IMG = {"kind": "image", "prompt": "a lighthouse", "quality": "draft", "model_id": "z-image-turbo"}


async def test_bake_creates_snapshot_and_new_machines_boot_from_it(env):
    async with Harness(env, settings={"cache_policy": {"snapshot_regions": ["tor1", "nyc2"]}}) as h:
        await h.setup_admin()
        b = Baker(h.ctx, poll_s=0.05)
        run_id = await b.start()
        await b.run(run_id)
        rows = await h.sql("SELECT state, step FROM background_runs")
        assert rows[0][0] == "succeeded", rows
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
