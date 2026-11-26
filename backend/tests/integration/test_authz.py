"""Who may call what: every route is public, ticket-only, signed-in or admin-only on purpose, and a signed-in
user only ever reaches their own creations. New routes must be added to these lists, so the test keeps up."""

from __future__ import annotations

import importlib
import uuid

import httpx
from fastapi.routing import APIRoute
from sqlalchemy import select

from aivo.api.routes.events import events_for
from aivo.db.models import Event, User
from tests.integration.harness import HEADERS, Harness
from tests.integration.test_adult import member_client
from tests.integration.test_gallery import FOX, items, make

MODULES = [
    "assets",
    "auth",
    "catalog",
    "events",
    "health",
    "items",
    "jobs",
    "machines",
    "oidc",
    "settings",
    "spending",
    "worker",
]

PUBLIC = {
    ("GET", "/auth/status"),
    ("POST", "/auth/setup"),  # only while there are no users
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("POST", "/auth/accept-invite"),  # needs a valid, unused invite
    ("GET", "/health"),
    ("GET", "/auth/oidc/login"),
    ("GET", "/auth/oidc/callback"),
}
ADMIN = {
    ("GET", "/admin/users"),
    ("PATCH", "/admin/users/{user_id}"),
    ("POST", "/auth/invites"),
    ("GET", "/admin/jobs"),
    ("POST", "/admin/jobs/{job_id}/cancel"),
    ("GET", "/jobs/{job_id}/log"),
    ("POST", "/machines/gpus/{gpu_type:path}/keep-warm"),
    ("GET", "/settings"),
    ("PUT", "/settings"),
}


def _deps(d, acc: set[str]) -> set[str]:  # type: ignore[no-untyped-def]
    for sub in d.dependencies:
        acc.add(getattr(sub.call, "__name__", ""))
        _deps(sub, acc)
    return acc


def routes() -> list[tuple[str, str, set[str]]]:
    out = []
    for m in MODULES:
        mod = importlib.import_module(f"aivo.api.routes.{m}")
        for name in [n for n in dir(mod) if n.endswith("router")]:
            for r in getattr(mod, name).routes:
                if isinstance(r, APIRoute):
                    out += [(method, r.path, _deps(r.dependant, set())) for method in r.methods]
    return out


def test_every_route_says_who_may_use_it():
    seen = set()
    for method, path, deps in routes():
        key = (method, path)
        seen.add(key)
        if key in PUBLIC:
            assert not deps & {"current_user", "require_admin", "ticket_job"}, key
        elif path.startswith("/worker/"):
            assert "ticket_job" in deps, key  # a worker's one-time ticket for that one job
        else:
            assert "current_user" in deps, f"{key} has no sign-in check"
            assert ("require_admin" in deps) == (key in ADMIN), f"{key}: admin-only must be on purpose"
    assert PUBLIC | ADMIN <= seen, (PUBLIC | ADMIN) - seen


def _url(path: str) -> str:
    out = []
    for part in path.split("/"):
        if part.startswith("{"):
            part = "x" if part.startswith(("{gpu_type", "{name")) else str(uuid.uuid4())
        out.append(part)
    return "/api/v1" + "/".join(out)


async def test_signed_out_callers_and_members_are_refused(env):
    async with Harness(env) as h:
        await h.setup_admin()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=h.app), base_url="http://test", headers=HEADERS
        ) as anon:
            for method, path, _ in routes():
                if (method, path) in PUBLIC:
                    continue
                r = await anon.request(method, _url(path), json={} if method in ("POST", "PUT", "PATCH") else None)
                assert r.status_code == 401, (method, path, r.status_code, r.text[:200])
        async with member_client(h) as member:
            for method, path in ADMIN:
                r = await member.request(method, _url(path), json={} if method in ("POST", "PUT", "PATCH") else None)
                assert r.status_code == 403, (method, path, r.status_code, r.text[:200])
        assert (
            await h.client.post(
                "/api/v1/auth/setup", json={"email": "x@example.com", "display_name": "X", "password": "y" * 12}
            )
        ).status_code == 403


async def test_nobody_reaches_another_users_creations_through_any_route(env):
    async with Harness(env) as h:
        await h.setup_admin()
        admin = h.client
        theirs = await make(admin, FOX)
        [their_item] = await items(admin)
        async with member_client(h) as member:
            mine = await make(member, FOX)
            jid, aid = theirs["id"], their_item["id"]
            # the creation, its files and everything that can be done to it
            assert (await member.get(f"/api/v1/jobs/{jid}")).status_code == 404
            assert (await member.post(f"/api/v1/jobs/{jid}/cancel")).status_code == 404
            assert (await member.post(f"/api/v1/jobs/{jid}/retry")).status_code == 404
            assert (await member.get(f"/api/v1/jobs/{jid}/log")).status_code == 403
            assert (await member.delete(f"/api/v1/jobs/{jid}")).status_code == 404
            assert (await member.get(their_item["media"]["url"])).status_code == 404
            assert (await member.get(f"/api/v1/items/{aid}")).status_code == 404
            assert (
                await member.put(f"/api/v1/items/{aid}/collections", json={"collection_ids": []})
            ).status_code == 404
            # starting from it, or asking what starting from it would cost: like a missing picture
            [my_item] = await items(member)
            animate = {"kind": "video", "prompt": "it moves"}
            assert (
                await member.post("/api/v1/estimate", json={**animate, "reference_asset_id": my_item["id"]})
            ).status_code == 200
            for path in ("/api/v1/estimate", "/api/v1/jobs"):
                r = await member.post(path, json={**animate, "reference_asset_id": aid})
                assert r.status_code == 422 and "could not be found" in r.text, (path, r.text)
            # lists that mention creations: only your own
            spend = (await member.get("/api/v1/spending")).json()
            assert {j["id"] for j in spend["recent_jobs"]} == {mine["id"]}
            assert {j["id"] for j in (await admin.get("/api/v1/spending")).json()["recent_jobs"]} == {jid, mine["id"]}
            # live updates: a creation's events reach its owner only, admins included
            async with h.ctx.sessionmaker() as s:
                for email, own, other in (
                    ("member@example.com", mine["id"], jid),
                    ("admin@example.com", jid, mine["id"]),
                ):
                    u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                    refs = {
                        str(r)
                        for r in (
                            await s.execute(select(Event.ref_id).where(events_for(u), Event.scope == "job"))
                        ).scalars()
                    }
                    assert own in refs and other not in refs, (email, refs)


async def test_which_creation_a_gpu_is_on_is_for_admins_only(env):
    from aivo.providers.runpod.fake import FakeRunPod

    rp = FakeRunPod(boot_seconds=env.fake_boot_seconds, job_seconds=30, max_workers_total=20)
    async with Harness(env, runpod=rp) as h:
        await h.setup_admin()
        r = await h.client.post("/api/v1/jobs", json=FOX)
        jid = r.json()["id"]

        async def on_a_gpu():  # type: ignore[no-untyped-def]
            ws = (await h.client.get("/api/v1/machines")).json()["workers"]
            return any(w["current_job_id"] == jid for w in ws)

        await h.wait_for(on_a_gpu, timeout=20)
        async with member_client(h) as member:
            ws = (await member.get("/api/v1/machines")).json()["workers"]
            assert ws and all(w["current_job_id"] is None for w in ws)
        await h.client.post(f"/api/v1/jobs/{jid}/cancel")
