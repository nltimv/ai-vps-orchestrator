"""End-to-end: API -> scheduler -> fake machine (real TLS mock worker) -> encrypted gallery."""

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
        assert est["machine"]["gpu_class"] == "rtx4000ada", est
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

        machines = (await h.client.get("/api/v1/machines")).json()
        assert len(machines["live"]) == 1 and machines["live"][0]["state"] == "ready"

        # worker deleted its copies after we acknowledged them
        m = next(iter(h.provider.machines.values()))
        assert not any((m.root / "jobs").glob(f"{job['id']}*"))


async def test_video_and_music_reuse_machine(env):
    async with Harness(env, settings={"max_instances": 1}) as h:
        await h.setup_admin()
        v = (
            await h.client.post(
                "/api/v1/jobs",
                json={
                    "kind": "video",
                    "prompt": "waves at sunset",
                    "quality": "standard",
                    "model_id": "ltx2",
                    "duration": "short",
                },
            )
        ).json()
        mu = (
            await h.client.post(
                "/api/v1/jobs",
                json={"kind": "music", "prompt": "lofi hip hop, mellow piano", "duration": "30s", "lyrics": "la la la"},
            )
        ).json()
        vd = await h.wait_job(v["id"], timeout=60)
        md = await h.wait_job(mu["id"], timeout=60)
        assert vd["state"] == "succeeded", vd
        assert md["state"] == "succeeded", md
        assert vd["outputs"][0]["media_type"] == "video/mp4"
        assert md["outputs"][0]["media_type"] == "audio/mp4"
        # range on video for seeking
        url = vd["outputs"][0]["url"]
        size = vd["outputs"][0]["bytes"]
        r = await h.client.get(url, headers={"Range": f"bytes={size // 2}-"})
        assert r.status_code == 206 and len(r.content) == size - size // 2
        # max_instances=1: one machine served both
        rows = await h.sql("SELECT count(*) FROM instances")
        assert rows[0][0] == 1


async def test_idle_machine_is_destroyed(env):
    async with Harness(env, settings={"idle_timeout_s": 1}) as h:
        await h.setup_admin()
        j = (await h.client.post("/api/v1/jobs", json={"kind": "image", "prompt": "a cat", "quality": "draft"})).json()
        assert (await h.wait_job(j["id"]))["state"] == "succeeded"

        async def gone():
            rows = await h.sql("SELECT state FROM instances")
            return rows and all(r[0] == "destroyed" for r in rows)

        await h.wait_for(gone, timeout=15)
        assert h.provider.machines == {}
        # cost was accrued
        rows = await h.sql("SELECT sum(usd) FROM cost_accruals")
        assert rows[0][0] is not None and float(rows[0][0]) >= 0


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


async def test_prewarm_only_when_enabled(env):
    async with Harness(env) as h:
        await h.setup_admin()
        r = (await h.client.post("/api/v1/machines/prewarm", json={"kind": "image"})).json()
        assert r == {"started": False, "reason": "disabled"}
        await h.client.put("/api/v1/settings", json={"speculative_warmup": True})
        r = (await h.client.post("/api/v1/machines/prewarm", json={"kind": "image"})).json()
        assert r["started"] is True

        async def ready():
            rows = await h.sql("SELECT state FROM instances")
            return rows and rows[0][0] == "ready"

        await h.wait_for(ready, timeout=15)
        r = (await h.client.post("/api/v1/machines/prewarm", json={"kind": "image"})).json()
        assert r == {"started": False, "reason": "already_running"}
