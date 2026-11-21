"""Machine log stream, start-up reporting and the brake on repeated start-up failures (fake provider)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from aivo.services.machine_log import Cursor, stream
from tests.integration.harness import HEADERS, Harness

IMG = {"kind": "image", "prompt": "a lighthouse at dusk", "quality": "draft", "model_id": "z-image-turbo"}


async def _never() -> bool:
    return False


async def collect(h: Harness, iid: str, until, cursor: Cursor | None = None, timeout: float = 20) -> list[dict]:  # type: ignore[no-untyped-def]
    """Read the machine log stream until ``until(lines)`` is true; returns all messages."""
    import uuid

    msgs: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    gen = stream(h.ctx, uuid.UUID(iid), cursor or Cursor(), _never)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            m = await asyncio.wait_for(gen.__anext__(), max(0.1, deadline - time.monotonic()))
            msgs.append(m)
            if m["event"] == "lines":
                lines += m["data"]["lines"]
            if m["event"] == "end" or until(lines):
                return msgs
    except (StopAsyncIteration, TimeoutError):
        pass
    finally:
        await gen.aclose()
    raise AssertionError(f"log condition not met; got {[(ln['source'], ln['text']) for ln in lines]}")


def all_lines(msgs: list[dict]) -> list[dict]:
    return [ln for m in msgs if m["event"] == "lines" for ln in m["data"]["lines"]]


def sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        ev, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            out.append((ev, json.loads("\n".join(data))))
    return out


async def machine_id(h: Harness) -> str:
    async def one() -> str | None:
        rows = await h.sql("SELECT id FROM instances ORDER BY created_at DESC LIMIT 1")
        return str(rows[0][0]) if rows else None

    return await h.wait_for(one)


async def test_log_streams_history_setup_agent_and_engine_lines(env):
    async with Harness(env) as h:
        await h.setup_admin()
        h.provider.job_seconds = 1.0
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        iid = await machine_id(h)
        msgs = await collect(h, iid, lambda ls: any("done in" in ln["text"] for ln in ls if ln["source"] == "agent"))
        lines = all_lines(msgs)
        by = {src: [ln["text"] for ln in lines if ln["source"] == src] for src in ("aivo", "setup", "agent", "engine")}
        assert any(t.startswith("Start-up step: ") for t in by["aivo"]), by["aivo"]  # sampled while starting
        assert any("created" in t for t in by["aivo"])
        assert any("== step: preparing_disks" in t for t in by["setup"])
        assert any("AI engine ready" in t for t in by["agent"])
        assert any("received: model z-image-turbo" in t for t in by["agent"])
        assert any(t == "got prompt" for t in by["engine"])
        assert not any("lighthouse" in ln["text"] for ln in lines), "prompts must never reach the log"
        assert any(m["event"] == "status" and m["data"]["phase"] == "live" for m in msgs)
        # machine lines arrive in order, without gaps or repeats
        seqs = [ln["seq"] for ln in lines if "seq" in ln]
        assert seqs == sorted(set(seqs)) and seqs[0] == 1
        assert (await h.wait_job(j["id"]))["state"] == "succeeded"

        # resuming from the last cursor only yields what is new
        last = Cursor.parse(msgs[-1]["id"])
        assert last.machine == seqs[-1]
        await h.client.post(f"/api/v1/machines/{iid}/keep-warm", json={"minutes": 30})
        more = await collect(h, iid, lambda ls: any("Keep warm" in ln["text"] for ln in ls), cursor=last)
        new = all_lines(more)
        assert all(ln.get("seq", 10**9) > last.machine for ln in new if ln["source"] != "aivo")
        assert not any("Start-up step" in ln["text"] for ln in new)


async def test_log_endpoint_is_admin_only_and_keeps_the_last_lines(env):
    async with Harness(env) as h:
        await h.setup_admin()
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        assert (await h.wait_job(j["id"]))["state"] == "succeeded"
        iid = await machine_id(h)

        inv = (await h.client.post("/api/v1/auth/invites", json={"role": "member"})).json()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=h.app), base_url="http://test", headers=HEADERS
        ) as member:
            r = await member.post(
                "/api/v1/auth/accept-invite",
                json={"token": inv["token"], "email": "m@example.com", "display_name": "M", "password": "x" * 12},
            )
            assert r.status_code == 200, r.text
            assert (await member.get(f"/api/v1/machines/{iid}/logs")).status_code == 403
            listing = (await member.get("/api/v1/machines")).json()
            assert "provisioning_paused" in listing

        assert (await h.client.get("/api/v1/machines/00000000-0000-0000-0000-000000000000/logs")).status_code == 404

        await h.client.post(f"/api/v1/machines/{iid}/shutdown")

        async def destroyed() -> bool:
            return (await h.sql("SELECT state FROM instances WHERE id = :i", i=iid))[0][0] == "destroyed"

        await h.wait_for(destroyed, timeout=20)
        recent = (await h.client.get("/api/v1/machines")).json()["recent"]
        assert recent[0]["has_saved_log"] is True
        r = await h.client.get(f"/api/v1/machines/{iid}/logs")
        assert r.headers["content-type"].startswith("text/event-stream")
        events = sse(r.text)
        assert events[-1][0] == "end"
        saved = [ln for ev, d in events if ev == "lines" and d.get("saved") for ln in d["lines"]]
        assert any(ln["source"] == "agent" for ln in saved)


async def test_reported_start_up_failure_fails_fast_then_pauses_new_machines(env):
    async with Harness(env, settings={"requeue_backoff_s": 0}) as h:
        await h.setup_admin()
        h.provider.chaos["*"] = ["boot_reports_failure", "boot_reports_failure"]

        j1 = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        d1 = await h.wait_job(j1["id"])
        assert d1["state"] == "failed" and "no GPU driver" in d1["error"], d1
        assert h.provider.create_calls == 1  # the machine said what is wrong: no retry
        iid = await machine_id(h)
        row = (await h.sql("SELECT boot_log, failure_reason FROM instances WHERE id = :i", i=iid))[0]
        assert any("FAILED" in ln["text"] for ln in row[0]) and "no GPU driver" in row[1]
        assert (await h.client.get("/api/v1/machines")).json()["provisioning_paused"] is None

        j2 = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        assert (await h.wait_job(j2["id"]))["state"] == "failed"
        paused = (await h.client.get("/api/v1/machines")).json()["provisioning_paused"]
        assert paused and "no GPU driver" in paused["reason"]
        assert (await h.client.get("/api/v1/health")).json()["provisioning_paused"] is True

        # paused: new jobs fail right away and no machine is started
        j3 = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        d3 = await h.wait_job(j3["id"])
        assert d3["state"] == "failed" and "paused" in d3["error"]
        assert h.provider.create_calls == 2
        r = await h.client.post("/api/v1/machines/warmup", json={"kind": "image"})
        assert r.status_code == 409

        # the saved log of the failed machine is readable after it is gone
        r = await h.client.get(f"/api/v1/machines/{iid}/logs")
        texts = [ln["text"] for ev, d in sse(r.text) if ev == "lines" for ln in d["lines"]]
        assert any("Failure:" in t for t in texts) and any("== step: checking_gpu" in t for t in texts)

        assert (await h.client.post("/api/v1/machines/provisioning/resume")).json() == {"resumed": True}
        j4 = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        assert (await h.wait_job(j4["id"]))["state"] == "succeeded"
        assert (await h.client.get("/api/v1/machines")).json()["provisioning_paused"] is None


async def test_engine_that_cannot_start_is_reported(env):
    async with Harness(env, settings={"boot_failures_before_pause": 0}) as h:
        await h.setup_admin()
        h.provider.chaos["*"] = ["engine_start_fail"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        d = await h.wait_job(j["id"])
        assert d["state"] == "failed" and "AI engine could not start" in d["error"], d
        assert h.provider.create_calls == 1
        iid = await machine_id(h)
        log = (await h.sql("SELECT boot_log FROM instances WHERE id = :i", i=iid))[0][0]
        assert any(ln["source"] == "engine" and "CUDA driver" in ln["text"] for ln in log)


async def test_silent_machine_times_out_and_is_retried_once(env):
    settings = {"boot_health_timeout_base_s": 2, "boot_health_max_s": 3, "requeue_backoff_s": 0}
    async with Harness(env, settings=settings) as h:
        await h.setup_admin()
        h.provider.chaos["*"] = ["boot_silent", "boot_silent", "boot_silent"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        d = await h.wait_job(j["id"], timeout=40)
        assert d["state"] == "failed" and "never answered" in d["error"], d
        assert h.provider.create_calls == 2  # one retry, not more
        assert (await h.client.get("/api/v1/machines")).json()["provisioning_paused"] is not None


async def test_slow_start_up_that_keeps_progressing_is_not_cut_off(env):
    settings = {"boot_health_timeout_base_s": 2, "boot_progress_grace_s": 3, "boot_health_max_s": 60}
    async with Harness(env, settings=settings) as h:
        await h.setup_admin()
        h.provider.slow_step_s = 1.2  # 4 steps: ~5 s, well past the 2 s base budget
        h.provider.chaos["*"] = ["boot_slow_progress"]
        j = (await h.client.post("/api/v1/jobs", json=IMG)).json()
        assert (await h.wait_job(j["id"], timeout=40))["state"] == "succeeded"
        stages = await h.sql("SELECT payload->>'stage' FROM events WHERE type = 'boot_stage' ORDER BY id")
        assert [s[0] for s in stages][:3] == ["preparing_disks", "checking_gpu", "installing_runtime"]
