"""End to end: API -> scheduler -> simulated RunPod queue (real worker code, mock engine) -> encrypted gallery."""

from tests.integration.harness import Harness


async def test_image_job_end_to_end(env):
    async with Harness(env) as h:
        me = await h.setup_admin()
        assert me["role"] == "admin"
        cat = (await h.client.get("/api/v1/catalog")).json()
        assert {m["id"] for m in cat["models"]} >= {"z-image-turbo", "wan22-a14b", "ace-step-15"}

        est = (
            await h.client.post(
                "/api/v1/estimate", json={"kind": "image", "prompt": "a red fox in snow", "quality": "draft"}
            )
        ).json()
        assert est["machine"]["gpu_class"] == "rtx4000ada", est  # the cheapest GPU type that fits
        assert est["est_cost_usd"] > 0 and est["est_wait_s"] > 0

        r = await h.client.post(
            "/api/v1/jobs",
            json={
                "kind": "image",
                "prompt": "a red fox in snow",
                "quality": "draft",
                "count": 2,
                "aspect": "portrait",
                "style": "photo",
            },
            headers={"Idempotency-Key": "abc"},
        )
        assert r.status_code == 201, r.text
        job = r.json()
        # idempotent re-submit returns the same job
        r2 = await h.client.post(
            "/api/v1/jobs", json={"kind": "image", "prompt": "x"}, headers={"Idempotency-Key": "abc"}
        )
        assert r2.json()["id"] == job["id"]

        done = await h.wait_job(job["id"])
        assert done["state"] == "succeeded", done
        assert len(done["outputs"]) == 2
        out = done["outputs"][0]
        assert out["media_type"] == "image/png" and out["thumb_url"]
        body = (await h.client.get(out["url"])).content
        assert body[:8] == b"\x89PNG\r\n\x1a\n"
        part = await h.client.get(out["url"], headers={"Range": "bytes=1-3"})
        assert part.status_code == 206 and part.content == b"PNG"
        thumb = await h.client.get(out["thumb_url"])
        assert thumb.content[:4] == b"RIFF"  # webp

        # nothing on the media volume is plaintext
        for p in env.media_dir.rglob("*"):
            if p.is_file():
                data = p.read_bytes()
                assert b"PNG" not in data[:64] and b"RIFF" not in data[:64] and b"ftyp" not in data[:64]
        # prompt is sealed in the DB
        rows = await h.sql("SELECT prompt_enc FROM jobs")
        assert all(b"fox" not in bytes(r[0]) for r in rows)

        gallery = (await h.client.get("/api/v1/gallery")).json()
        assert gallery["items"][0]["id"] == job["id"]
        assert gallery["items"][0]["prompt"] == "a red fox in snow"

        gpus = (await h.client.get("/api/v1/machines")).json()
        assert [g["gpu_type"] for g in gpus["gpus"]] == ["NVIDIA RTX 4000 Ada Generation"]
        assert gpus["gpus"][0]["status"] == "ready"
        assert len(gpus["workers"]) == 1 and gpus["workers"][0]["state"] in ("waiting", "working")

        # RunPod's job held only a ticket: no prompt, no picture
        fake = h.runpod
        (ep,) = fake.endpoints.values()
        (rp_job,) = ep.jobs
        assert set(rp_job.input) == {"aivo_url", "job_id", "token", "protocol"}
        assert "fox" not in str(rp_job.input)
        # ... and its result is just the file names (the files went straight to aivo)
        assert set(rp_job.output) == {"outputs"} and all(isinstance(n, str) for n in rp_job.output["outputs"])
        # the worker deleted its copies after delivering them
        (w,) = fake.workers_of(ep.id)
        assert not any(w.runtime.executor.job_root.rglob(f"{job['id']}*"))
        # the ticket stopped working when the job ended
        r = await h.client.post(
            f"/api/v1/worker/jobs/{job['id']}/progress",
            json={"stage": "generating", "progress": 0.5},
            headers={"Authorization": f"Bearer {rp_job.input['token']}"},
        )
        assert r.status_code == 401


async def test_one_endpoint_per_gpu_and_workers_are_reused(env):
    async with Harness(env, settings={"keep_warm_s": 30}) as h:
        await h.setup_admin()
        img = {"kind": "image", "prompt": "a cat", "quality": "draft"}
        first = await h.wait_job((await h.client.post("/api/v1/jobs", json=img)).json()["id"])
        second = await h.wait_job((await h.client.post("/api/v1/jobs", json=img)).json()["id"])
        assert first["state"] == second["state"] == "succeeded"
        # same GPU type: one endpoint, and the waiting worker took the second job (no second start)
        assert len(h.runpod.endpoints) == 1
        rows = await h.sql("SELECT count(*), count(DISTINCT provider_instance_id) FROM instances")
        assert rows[0] == (1, 1)
        assert first["instance_id"] == second["instance_id"]
        # each job is charged its share of the worker's time; the second one no cold start
        assert first["cost_usd"] > 0 and second["cost_usd"] > 0
        # a different GPU type gets its own endpoint
        vid = {
            "kind": "video",
            "prompt": "waves at sunset",
            "quality": "standard",
            "model_id": "ltx2",
            "duration": "short",
        }
        v = await h.wait_job((await h.client.post("/api/v1/jobs", json=vid)).json()["id"], timeout=60)
        assert v["state"] == "succeeded", v
        assert v["outputs"][0]["media_type"] == "video/mp4"
        assert len(h.runpod.endpoints) == 2
        url, size = v["outputs"][0]["url"], v["outputs"][0]["bytes"]
        r = await h.client.get(url, headers={"Range": f"bytes={size // 2}-"})
        assert r.status_code == 206 and len(r.content) == size - size // 2


async def test_parallel_jobs_get_their_own_workers(env):
    async with Harness(env, settings={"max_workers_per_gpu": 2}) as h:
        await h.setup_admin()
        img = {"kind": "image", "prompt": "a cat", "quality": "draft"}
        ids = [(await h.client.post("/api/v1/jobs", json=img)).json()["id"] for _ in range(3)]
        done = [await h.wait_job(i, timeout=60) for i in ids]
        assert all(d["state"] == "succeeded" for d in done), done
        workers = (await h.sql("SELECT count(DISTINCT provider_instance_id) FROM instances"))[0][0]
        assert workers == 2  # the endpoint's maximum; the third job waited for a free worker


async def test_idle_worker_stops_and_billing_ends(env):
    async with Harness(env, settings={"keep_warm_s": 5}) as h:
        await h.setup_admin()
        j = (await h.client.post("/api/v1/jobs", json={"kind": "image", "prompt": "a cat", "quality": "draft"})).json()
        assert (await h.wait_job(j["id"]))["state"] == "succeeded"

        async def ended():
            rows = await h.sql("SELECT state, destroy_confirmed_at - idle_since FROM instances")
            return rows and all(r[0] == "destroyed" for r in rows) and rows

        rows = await h.wait_for(ended, timeout=20)
        assert rows[0][1].total_seconds() == 5  # billed until RunPod stopped it: keep-warm after its last job
        rows = await h.sql("SELECT sum(usd) FROM cost_accruals")
        assert rows[0][0] is not None and float(rows[0][0]) > 0


async def test_unauthenticated_and_csrf(env):
    async with Harness(env) as h:
        r = await h.client.get("/api/v1/jobs")
        assert r.status_code == 401
        r = await h.client.post(
            "/api/v1/auth/login", json={"email": "a@b.c", "password": "x"}, headers={"X-Requested-With": ""}
        )
        assert r.status_code == 403
        await h.setup_admin()
        r = await h.client.post(
            "/api/v1/auth/setup", json={"email": "x@example.com", "display_name": "X", "password": "another password"}
        )
        assert r.status_code == 403
