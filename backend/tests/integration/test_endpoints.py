"""RunPod endpoints change only when their settings do: each change is a new RunPod release, and its workers
start over (a FlashBoot-prepared worker is lost)."""

from __future__ import annotations

from urllib.parse import quote

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
