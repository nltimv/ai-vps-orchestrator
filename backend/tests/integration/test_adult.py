"""Adult (18+) engines: who may use them, who may see the results, and the fixed safeguards."""

from __future__ import annotations

import contextlib
import io
from collections.abc import AsyncIterator

import httpx
from PIL import Image

from tests.integration.harness import HEADERS, Harness

ADULT_IMG = {"kind": "image", "prompt": "a woman on a beach at sunset", "quality": "draft", "model_id": "chroma1-hd"}


@contextlib.asynccontextmanager
async def member_client(h: Harness) -> AsyncIterator[httpx.AsyncClient]:
    inv = (await h.client.post("/api/v1/auth/invites", json={"role": "member"})).json()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app), base_url="http://test", headers=HEADERS, timeout=30
    ) as c:
        r = await c.post(
            "/api/v1/auth/accept-invite",
            json={"token": inv["token"], "email": "member@example.com", "display_name": "M", "password": "x" * 12},
        )
        assert r.status_code == 200, r.text
        yield c


async def allow_adult(h: Harness) -> None:
    assert (await h.client.put("/api/v1/settings", json={"adult_content_enabled": True})).status_code == 200
    me = (await h.client.get("/api/v1/auth/me")).json()
    assert (await h.client.patch(f"/api/v1/admin/users/{me['id']}", json={"adult_allowed": True})).status_code == 200


def adult_engines(catalog: dict) -> set[str]:
    return {m["id"] for m in catalog["models"] if m["adult"]}


async def test_adult_engines_need_the_setting_and_the_account_switch(env):
    async with Harness(env) as h:
        await h.setup_admin()
        assert adult_engines((await h.client.get("/api/v1/catalog")).json()) == set()
        assert (await h.client.post("/api/v1/jobs", json=ADULT_IMG)).status_code == 403
        # the global switch alone isn't enough, also not for admins
        await h.client.put("/api/v1/settings", json={"adult_content_enabled": True})
        assert adult_engines((await h.client.get("/api/v1/catalog")).json()) == set()
        assert (await h.client.post("/api/v1/estimate", json=ADULT_IMG)).status_code == 403
        me = (await h.client.get("/api/v1/auth/me")).json()
        assert me["adult_content"] is False
        await h.client.patch(f"/api/v1/admin/users/{me['id']}", json={"adult_allowed": True})
        assert (await h.client.get("/api/v1/auth/me")).json()["adult_content"] is True
        assert adult_engines((await h.client.get("/api/v1/catalog")).json()) == {"chroma1-hd", "wan22-i2v-adult"}
        # "Auto" never picks an adult engine
        auto = (await h.client.post("/api/v1/estimate", json={**ADULT_IMG, "model_id": None})).json()
        assert auto["model_id"] != "chroma1-hd"


async def test_adult_creations_are_hidden_from_everyone_else(env):
    async with Harness(env) as h:
        await h.setup_admin()
        await allow_adult(h)
        j = (await h.client.post("/api/v1/jobs", json=ADULT_IMG)).json()
        done = await h.wait_job(j["id"])
        assert done["state"] == "succeeded"
        assert (await h.sql("SELECT adult FROM jobs WHERE id = :i", i=j["id"]))[0][0] is True
        asset_url = done["outputs"][0]["url"]
        assert (await h.client.get(asset_url)).status_code == 200

        async with member_client(h) as member:
            gallery = (await member.get("/api/v1/gallery")).json()["items"]
            assert j["id"] not in {i["id"] for i in gallery}
            assert (await member.get(f"/api/v1/jobs/{j['id']}")).status_code == 404
            assert (await member.get(asset_url)).status_code == 404
            assert (await member.get(done["outputs"][0]["thumb_url"] or asset_url)).status_code == 404
            assert adult_engines((await member.get("/api/v1/catalog")).json()) == set()

        # switching adult content off hides it from the allowed account too
        await h.client.put("/api/v1/settings", json={"adult_content_enabled": False})
        assert (await h.client.get(f"/api/v1/jobs/{j['id']}")).status_code == 404
        assert j["id"] not in {i["id"] for i in (await h.client.get("/api/v1/gallery")).json()["items"]}
        assert j["id"] not in {i["id"] for i in (await h.client.get("/api/v1/jobs")).json()}


async def test_descriptions_involving_minors_are_refused(env):
    async with Harness(env) as h:
        await h.setup_admin()
        await allow_adult(h)
        for prompt in ("a teenager at the beach", "a 16 year old", "schoolgirl in uniform", "een meisje"):
            r = await h.client.post("/api/v1/estimate", json={**ADULT_IMG, "prompt": prompt})
            assert r.status_code == 422 and "minors" in r.json()["detail"], (prompt, r.text)
            assert (await h.client.post("/api/v1/jobs", json={**ADULT_IMG, "prompt": prompt})).status_code == 422
        assert (
            await h.client.post("/api/v1/estimate", json={**ADULT_IMG, "prompt": "a 25 year old woman"})
        ).status_code == 200
        assert (await h.sql("SELECT count(*) FROM jobs"))[0][0] == 0


async def test_animation_only_starts_from_pictures_made_from_text(env):
    async with Harness(env) as h:
        await h.setup_admin()
        await allow_adult(h)
        made = await h.wait_job((await h.client.post("/api/v1/jobs", json=ADULT_IMG)).json()["id"])
        picture = made["outputs"][0]["id"]
        animate = {"kind": "video", "prompt": "she turns and smiles", "quality": "draft", "model_id": "wan22-i2v-adult"}

        ok = (await h.client.post("/api/v1/jobs", json={**animate, "reference_asset_id": picture})).json()
        done = await h.wait_job(ok["id"], timeout=60)
        assert done["state"] == "succeeded"
        assert (await h.sql("SELECT adult, mode FROM jobs WHERE id = :i", i=ok["id"]))[0] == (True, "i2v")

        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (10, 20, 30)).save(buf, format="PNG")
        upload = (await h.client.post("/api/v1/uploads", files={"file": ("p.png", buf.getvalue(), "image/png")})).json()
        r = await h.client.post("/api/v1/jobs", json={**animate, "reference_asset_id": upload["id"]})
        assert r.status_code == 422 and "never uploaded photos" in r.json()["detail"]

        # an edit of an uploaded photo doesn't count as made from text either
        edit = (
            await h.client.post(
                "/api/v1/jobs",
                json={
                    "kind": "image",
                    "prompt": "make it sunset",
                    "quality": "draft",
                    "reference_asset_id": upload["id"],
                },
            )
        ).json()
        edited = await h.wait_job(edit["id"])
        r = await h.client.post("/api/v1/jobs", json={**animate, "reference_asset_id": edited["outputs"][0]["id"]})
        assert r.status_code == 422


async def test_normal_engines_keep_the_adult_mark(env):
    """Animating an adult picture with a normal engine is adult content too, and needs the permission."""
    async with Harness(env) as h:
        await h.setup_admin()
        await allow_adult(h)
        made = await h.wait_job((await h.client.post("/api/v1/jobs", json=ADULT_IMG)).json()["id"])
        normal = {
            "kind": "video",
            "prompt": "slow camera move",
            "quality": "draft",
            "model_id": "wan22-a14b",
            "reference_asset_id": made["outputs"][0]["id"],
        }
        j = (await h.client.post("/api/v1/jobs", json=normal)).json()
        assert (await h.sql("SELECT adult FROM jobs WHERE id = :i", i=j["id"]))[0][0] is True
        r = await h.client.post("/api/v1/jobs", json={**normal, "prompt": "a teen walks by"})
        assert r.status_code == 422  # the minors check applies to every adult creation

        async with member_client(h) as member:
            # can't even reference it: as far as the member knows, it doesn't exist
            r = await member.post("/api/v1/jobs", json=normal)
            assert r.status_code == 404
