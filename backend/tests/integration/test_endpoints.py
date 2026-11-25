"""RunPod endpoints change only when their settings do: each change is a new RunPod release, and its workers
start over (a FlashBoot-prepared worker is lost)."""

from __future__ import annotations

from urllib.parse import quote

from sqlalchemy import text

from aivo.controller.common import get_offers
from tests.integration.harness import Harness


async def test_stock_moving_between_data_centers_changes_nothing_and_keep_ready_only_the_workers(env):
    async with Harness(env) as h:
        await h.setup_admin()
        a = (await h.client.post("/api/v1/jobs", json={"kind": "image", "prompt": "a fox", "quality": "draft"})).json()
        assert (await h.wait_job(a["id"]))["state"] == "succeeded"
        rp = h.runpod
        [ep] = rp.endpoints.values()
        gpu_type = ep.body["env"]["AIVO_GPU_TYPE"]
        # every data center in the allowed countries, whether it has stock now or not; none elsewhere
        assert ep.body["dataCenterIds"] == ["EU-NL-1", "EU-RO-1", "EUR-IS-1"]
        sync = h.ctx.controller.housekeeping.endpoints.sync
        await sync()
        assert rp.updates == []

        rp.stock = {gpu_type: ["EUR-IS-1"]}  # its stock moved from the Netherlands to Iceland
        await get_offers(h.ctx, force=True)
        await sync()
        assert rp.updates == [], "a stock change must not update the endpoint"

        r = await h.client.post(f"/api/v1/machines/gpus/{quote(gpu_type)}/keep-warm", json={"minutes": 30})
        assert r.status_code == 200, r.text
        assert rp.updates == [(ep.id, ["workers"])] and ep.body["workers"]["min"] == 1
        await sync()
        assert len(rp.updates) == 1


async def test_an_endpoint_deleted_at_runpod_is_made_again_under_a_free_name(env):
    """RunPod keeps a deleted endpoint's name taken ("endpoint template names must be unique")."""
    async with Harness(env) as h:
        await h.setup_admin()
        body = {"kind": "image", "prompt": "a fox", "quality": "draft"}
        a = (await h.client.post("/api/v1/jobs", json=body)).json()
        assert (await h.wait_job(a["id"]))["state"] == "succeeded"
        rp = h.runpod
        [first] = rp.endpoints.values()
        name = first.body["name"]
        assert (await h.sql("SELECT name FROM gpu_endpoints"))[0][0] == name
        rp.endpoints.pop(first.id)  # deleted in the RunPod console
        await h.ctx.controller.housekeeping.endpoints.sync()
        b = (await h.client.post("/api/v1/jobs", json=body)).json()
        assert (await h.wait_job(b["id"]))["state"] == "succeeded", "the name being taken must not fail creations"
        [again] = rp.endpoints.values()
        assert again.body["name"] == f"{name}-2"
        assert (await h.sql("SELECT name FROM gpu_endpoints"))[0][0] == f"{name}-2"


async def test_an_existing_endpoint_keeps_its_name(env):
    """Endpoints made before aivo kept names (or under an older naming) aren't renamed."""
    async with Harness(env) as h:
        await h.setup_admin()
        a = (await h.client.post("/api/v1/jobs", json={"kind": "image", "prompt": "a fox", "quality": "draft"})).json()
        assert (await h.wait_job(a["id"]))["state"] == "succeeded"
        rp = h.runpod
        [ep] = rp.endpoints.values()
        ep.body["name"] = "aivo-older-name-cut-off-at-forty-charact"
        async with h.ctx.sessionmaker() as s, s.begin():
            await s.execute(text("UPDATE gpu_endpoints SET name = NULL, config_hash = 'from an older version'"))
        await h.ctx.controller.housekeeping.endpoints.sync()
        assert all("name" not in fields for _, fields in rp.updates), rp.updates
        assert (await h.sql("SELECT name FROM gpu_endpoints"))[0][0] == "aivo-older-name-cut-off-at-forty-charact"
        await h.ctx.controller.housekeeping.endpoints.sync()
        assert len(rp.updates) <= 1  # settled: the next sync sends nothing
