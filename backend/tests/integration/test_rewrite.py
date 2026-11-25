"""Improving the description on the GPU before creating (catalog/rewriting.yaml, worker protocol 2.1)."""

from __future__ import annotations

from aivo.catalog.presets import PROMPT_MARKER
from tests.integration.harness import Harness
from tests.integration.test_adult import allow_adult

FOX = {
    "kind": "image",
    "prompt": "a red fox in snow",
    "quality": "draft",
    "model_id": "z-image-turbo",
    "style": "photo",
}


def prompt_of(workflow: dict) -> str:
    """The positive prompt the engine got (z-image-turbo / chroma: the CLIPTextEncode with the description)."""
    texts = [n["inputs"]["text"] for n in workflow.values() if n.get("class_type") == "CLIPTextEncode"]
    return texts[0]


async def create(h: Harness, body: dict) -> dict:
    r = await h.client.post("/api/v1/jobs", json=body)
    assert r.status_code == 201, r.text
    return await h.wait_job(r.json()["id"])


async def test_description_is_improved_on_the_gpu_and_kept_sealed(env):
    async with Harness(env) as h:
        await h.setup_admin()
        done = await create(h, FOX)
        assert done["state"] == "succeeded", done
        used = done["prompt_used"]
        assert used and used.startswith("A red fox in snow, shown in rich detail"), used
        assert "<think>" not in used
        assert done["prompt"] == "a red fox in snow"  # the user's own words stay as written
        ran = prompt_of(h.runpod.workflows[done["id"]])
        assert ran == used and PROMPT_MARKER not in str(h.runpod.workflows[done["id"]])
        # sealed in the database; no copy of the words in the unencrypted parts of the job
        row = (await h.sql("SELECT prompt_used_enc, resolved::text, log::text FROM jobs"))[0]
        assert b"fox" not in bytes(row[0])
        assert "fox" not in row[1] and "fox" not in row[2]
        assert "description improved" in row[2]


async def test_switched_off_uses_the_description_as_written(env):
    async with Harness(env) as h:
        await h.setup_admin()
        done = await create(h, {**FOX, "enhance": False})
        assert done["state"] == "succeeded" and done["prompt_used"] is None
        assert done["params"]["enhance"] is False
        assert prompt_of(h.runpod.workflows[done["id"]]).startswith("a red fox in snow, photorealistic")


async def test_engines_without_rewriting_are_left_alone(env):
    async with Harness(env) as h:
        await h.setup_admin()
        done = await create(h, {"kind": "music", "prompt": "calm lo-fi piano", "duration": "30s"})
        assert done["state"] == "succeeded" and done["prompt_used"] is None
        assert "calm lo-fi piano" in str(h.runpod.workflows[done["id"]])


async def test_a_declining_or_failing_language_model_falls_back(env):
    async with Harness(env) as h:
        await h.setup_admin()
        for chaos in ("rewrite_refused", "rewrite_fail"):
            h.runpod.chaos = [chaos]
            done = await create(h, FOX)
            assert done["state"] == "succeeded", (chaos, done)
            assert done["prompt_used"] is None, chaos
            assert prompt_of(h.runpod.workflows[done["id"]]).startswith("a red fox in snow, photorealistic"), chaos
            log = (await h.client.get(f"/api/v1/jobs/{done['id']}/log")).json()
            assert any("kept the description as written" in ln["text"] for ln in log["lines"]), chaos


async def test_adult_rules_apply_to_the_improved_description(env):
    async with Harness(env) as h:
        await h.setup_admin()
        await allow_adult(h)
        h.runpod.chaos = ["rewrite_minor"]
        body = {"kind": "image", "prompt": "a woman on a beach at sunset", "quality": "draft", "model_id": "chroma1-hd"}
        done = await create(h, body)
        assert done["state"] == "succeeded" and done["adult"]
        assert done["prompt_used"] is None
        ran = prompt_of(h.runpod.workflows[done["id"]])
        assert "teenager" not in ran and "a woman on a beach at sunset" in ran


async def test_several_pictures_each_get_their_own_improved_description(env):
    async with Harness(env) as h:
        await h.setup_admin()
        done = await create(h, {**FOX, "count": 3})
        assert done["state"] == "succeeded" and len(done["outputs"]) == 3, done
        used = done["prompts_used"]
        assert len(used) == 3 and len(set(used)) == 3, used  # steered three ways
        ran = [h.runpod.workflows[done["id"]], *h.runpod.workflows[f"{done['id']}:more"]]
        assert [prompt_of(w) for w in ran] == used  # each run made with its own
        assert done["prompt_used"] == used[0]
