"""Each user's own gallery: every picture is a creation of its own, with favorites, collections and endless
scrolling; nobody else sees it, admins included."""

from __future__ import annotations

import asyncio
import time

import httpx

from tests.integration.harness import Harness
from tests.integration.test_adult import member_client

FOX = {"kind": "image", "prompt": "a red fox in snow", "quality": "draft", "model_id": "z-image-turbo"}


async def make(c: httpx.AsyncClient, body: dict) -> dict:
    r = await c.post("/api/v1/jobs", json=body)
    assert r.status_code == 201, r.text
    jid = r.json()["id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        j = (await c.get(f"/api/v1/jobs/{jid}")).json()
        if j["state"] in ("succeeded", "failed", "cancelled"):
            assert j["state"] == "succeeded", j
            return j
        await asyncio.sleep(0.1)
    raise AssertionError("creation didn't finish")


async def items(c: httpx.AsyncClient, **params: object) -> list[dict]:
    r = await c.get(
        "/api/v1/items", params={k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()}
    )
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_each_picture_is_its_own_creation_with_its_own_description_and_look(env):
    async with Harness(env) as h:
        await h.setup_admin()
        job = await make(h.client, {**FOX, "count": 3})
        got = await items(h.client)
        assert [i["job_id"] for i in got] == [job["id"]] * 3
        assert [i["index"] for i in got] == [0, 1, 2] and all(i["together"] == 3 for i in got)
        assert [i["id"] for i in got] == [o["id"] for o in job["outputs"]]
        details = [(await h.client.get(f"/api/v1/items/{i['id']}")).json() for i in got]
        assert [d["improved"] for d in details] == job["prompts_used"]  # each was made with its own
        assert [d["seed"] for d in details] == [job["seed"] + k for k in range(3)]
        assert [o["id"] for o in details[1]["job"]["outputs"]] == [i["id"] for i in got]  # made together


async def test_nobody_else_sees_a_gallery_not_even_an_admin(env):
    async with Harness(env) as h:
        await h.setup_admin()
        async with member_client(h) as member:
            mine = await make(member, FOX)
            [item] = await items(member)
            url = item["media"]["url"]
            assert (await member.get(url)).status_code == 200

            admin = h.client
            assert await items(admin) == []
            assert (await admin.get(f"/api/v1/items/{item['id']}")).status_code == 404
            assert (await admin.get(f"/api/v1/jobs/{mine['id']}")).status_code == 404
            assert (await admin.get(url)).status_code == 404
            assert (await admin.get(item["media"]["thumb_url"])).status_code == 404
            assert mine["id"] not in {j["id"] for j in (await admin.get("/api/v1/jobs")).json()}
            assert (await admin.put(f"/api/v1/items/{item['id']}/favorite", json={"favorite": True})).status_code == 404
            assert (await admin.delete(f"/api/v1/items/{item['id']}")).status_code == 404
            assert (await admin.delete(f"/api/v1/jobs/{mine['id']}")).status_code == 404

            # troubleshooting: how it ran, never what it shows
            [row] = [r for r in (await admin.get("/api/v1/admin/jobs")).json() if r["id"] == mine["id"]]
            assert row["user_name"] == "M" and row["state"] == "succeeded" and row["results"] == 1
            assert "fox" not in str(row)
            assert (await admin.get(f"/api/v1/jobs/{mine['id']}/log")).status_code == 200
            assert (await member.get("/api/v1/admin/jobs")).status_code == 403

            # and the other way round
            theirs = await make(admin, FOX)
            assert theirs["id"] not in {i["job_id"] for i in await items(member)}
            assert (await member.get(theirs["outputs"][0]["url"])).status_code == 404
            animate = {"kind": "video", "prompt": "", "reference_asset_id": theirs["outputs"][0]["id"]}
            assert (await member.post("/api/v1/jobs", json=animate)).status_code == 422  # not theirs to start from


async def test_favorites_and_collections(env):
    async with Harness(env) as h:
        await h.setup_admin()
        c = h.client
        await make(c, {**FOX, "count": 3})
        a, b, third = await items(c)

        assert (await c.put(f"/api/v1/items/{b['id']}/favorite", json={"favorite": True})).json() == {"favorite": True}
        assert [i["id"] for i in await items(c, favorites=True)] == [b["id"]]
        assert (await c.get(f"/api/v1/items/{b['id']}")).json()["item"]["favorite"] is True

        r = await c.post("/api/v1/collections", json={"name": "  Winter   walls "})
        assert r.status_code == 201, r.text
        walls = r.json()
        assert walls["name"] == "Winter walls" and walls["count"] == 0 and walls["cover_url"] is None
        assert (await c.post("/api/v1/collections", json={"name": "winter WALLS"})).status_code == 409
        best = (await c.post("/api/v1/collections", json={"name": "Best"})).json()

        bulk = {"ids": [a["id"], third["id"]], "action": "add", "collection_id": walls["id"]}
        assert (await c.post("/api/v1/items/bulk", json=bulk)).json() == {"count": 2}
        assert (await c.post("/api/v1/items/bulk", json=bulk)).json() == {"count": 2}  # twice is fine
        assert [i["id"] for i in await items(c, collection=walls["id"])] == [a["id"], third["id"]]
        cols = {x["name"]: x for x in (await c.get("/api/v1/collections")).json()}
        assert list(cols) == ["Best", "Winter walls"]  # by name
        assert cols["Winter walls"]["count"] == 2 and cols["Winter walls"]["cover_url"]

        # from an item's page: exactly these collections
        r = await c.put(f"/api/v1/items/{a['id']}/collections", json={"collection_ids": [best["id"]]})
        assert r.status_code == 200, r.text
        assert (await c.get(f"/api/v1/items/{a['id']}")).json()["collection_ids"] == [best["id"]]
        assert [i["id"] for i in await items(c, collection=walls["id"])] == [third["id"]]

        assert (await c.patch(f"/api/v1/collections/{walls['id']}", json={"name": "Walls"})).json()["name"] == "Walls"
        assert (await c.delete(f"/api/v1/collections/{walls['id']}")).status_code == 200
        assert len(await items(c)) == 3  # the pictures stay
        assert (await c.get("/api/v1/items", params={"collection": walls["id"]})).status_code == 404

        async with member_client(h) as member:  # someone else's collection can't be used or read
            await make(member, FOX)
            [mi] = await items(member)
            r = await member.post(
                "/api/v1/items/bulk", json={"ids": [mi["id"]], "action": "add", "collection_id": best["id"]}
            )
            assert r.status_code == 404
            assert (await member.get("/api/v1/items", params={"collection": best["id"]})).status_code == 404
            assert (await member.get("/api/v1/collections")).json() == []
            # and ids that aren't yours are left alone
            r = await member.post("/api/v1/items/bulk", json={"ids": [b["id"]], "action": "unfavorite"})
            assert r.json() == {"count": 0}
        assert (await c.get(f"/api/v1/items/{b['id']}")).json()["item"]["favorite"] is True


async def test_deleting_one_keeps_the_others_and_the_last_one_takes_the_words_along(env):
    async with Harness(env) as h:
        await h.setup_admin()
        c = h.client
        job = await make(c, {**FOX, "count": 3})
        first, middle, last = await items(c)
        col = (await c.post("/api/v1/collections", json={"name": "Keep"})).json()
        await c.post("/api/v1/items/bulk", json={"ids": [middle["id"]], "action": "add", "collection_id": col["id"]})

        assert (await c.delete(f"/api/v1/items/{middle['id']}")).status_code == 200
        left = await items(c)
        assert [i["id"] for i in left] == [first["id"], last["id"]]
        assert [i["index"] for i in left] == [0, 2] and all(i["together"] == 2 for i in left)  # places stay put
        assert (await c.get(middle["media"]["url"])).status_code == 404
        assert (await c.get("/api/v1/collections")).json()[0]["count"] == 0
        detail = (await c.get(f"/api/v1/items/{last['id']}")).json()
        assert detail["improved"] == job["prompts_used"][2]  # still its own description

        r = await c.post("/api/v1/items/bulk", json={"ids": [first["id"], last["id"]], "action": "delete"})
        assert r.json() == {"count": 2}
        assert await items(c) == []
        assert (await c.get(f"/api/v1/jobs/{job['id']}")).status_code == 404
        row = (await h.sql("SELECT prompt_enc, prompt_used_enc, deleted_at FROM jobs WHERE id = :i", i=job["id"]))[0]
        assert row[0] is None and row[1] is None and row[2] is not None


async def test_endless_scrolling_pages_through_everything_once_in_order(env):
    async with Harness(env) as h:
        await h.setup_admin()
        c = h.client
        older = await make(c, {**FOX, "count": 3})
        newer = await make(c, {**FOX, "kind": "image", "prompt": "a heron", "count": 3})
        seen, cursor = [], None
        while True:
            params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            page = (await c.get("/api/v1/items", params=params)).json()
            seen += page["items"]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert [i["id"] for i in seen] == [o["id"] for o in newer["outputs"]] + [o["id"] for o in older["outputs"]]
        assert (await c.get("/api/v1/items", params={"cursor": "not-a-cursor"})).status_code == 400
        assert await items(c, kind="video") == []
