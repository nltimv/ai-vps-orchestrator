"""Live machine log for admins (SSE): aivo's own decisions merged with the machine's log.

Sources:
* ``aivo``    - this backend: state changes, start-up steps, placement attempts (from the DB)
* ``setup``   - the machine's start-up script (status server first, then the agent's copy)
* ``agent``   - the worker agent
* ``engine``  - the AI engine (ComfyUI)

Nothing here is persisted except what already is (events, placement attempts, and the last
lines of machines that went away). The SSE ``id`` is a cursor ``"<event>:<attempt>:<machine line>"``
so a reconnecting browser resumes where it left off (``Last-Event-ID``).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select

from aivo.context import AppContext
from aivo.controller.common import worker_client
from aivo.controller.provisioner import stage_label
from aivo.db.models import Event, Instance, PlacementAttempt
from aivo.worker_client.client import WorkerUnreachable
from aivo_protocol.worker_api import LogsOut

UPSTREAM_WAIT_S = 15.0
REACHABLE_STATES = ("booting", "ready", "busy", "draining")
STATE_TEXT = {
    "pending": "Machine requested",
    "creating": "Asking the cloud for the machine",
    "provisioning": "The cloud accepted the request; waiting for the machine's address",
    "booting": "The machine has an address; waiting for it to start up",
    "ready": "Ready: the AI engine is up",
    "busy": "Working on a creation",
    "draining": "Shutting down after the current work",
    "destroying": "Destroying the machine",
    "destroyed": "Machine destroyed",
}
ATTEMPT_TEXT = {
    "created": "created",
    "capacity": "sold out, trying the next option",
    "quota": "the account's GPU limit is reached",
    "volume_busy": "the model disk is in use, starting without it",
    "error": "error",
    "unknown_adopted": "found the machine after a network hiccup",
    "late_failure": "took too long to get an address",
    "boot_failure": "start-up failed",
    "booted": "started",
}


@dataclass
class Cursor:
    event: int = 0
    attempt: int = 0
    machine: int = 0

    @classmethod
    def parse(cls, raw: str | None) -> Cursor:
        try:
            a, b, c = (int(x) for x in (raw or "").split(":"))
            return cls(max(0, a), max(0, b), max(0, c))
        except ValueError:
            return cls()

    def __str__(self) -> str:
        return f"{self.event}:{self.attempt}:{self.machine}"


def _ts(at: datetime) -> float:
    return at.timestamp()


def _aivo(ts: float, text: str, level: str | None = None) -> dict[str, Any]:
    return {"ts": ts, "source": "aivo", "level": level, "text": text}


def render_event(ev: Event) -> dict[str, Any] | None:
    p = ev.payload or {}
    if ev.type == "instance.state":
        state = p.get("state") or "?"
        if state == "pending" and p.get("retry"):
            return None  # back in line for the next option: the placement attempt already says so
        text = STATE_TEXT.get(state, f"State: {state}")
        if state == "creating" and p.get("size"):
            text += f" ({p['size']} in {p.get('region', '?')})"
        if p.get("reason"):
            text += f" (reason: {str(p['reason']).replace('_', ' ')})"
        if p.get("warmup"):
            text += " (started ahead of time)"
        return _aivo(_ts(ev.at), text)
    if ev.type == "boot_stage":
        detail = f": {p['detail']}" if p.get("detail") else ""
        return _aivo(_ts(ev.at), f"Start-up step: {p.get('label') or stage_label(p.get('stage'))}{detail}")
    if ev.type == "instance.keep_warm":
        return _aivo(_ts(ev.at), "Keep warm " + (f"until {p['until']}" if p.get("until") else "turned off"))
    return None


def render_attempt(a: PlacementAttempt) -> dict[str, Any]:
    what = ATTEMPT_TEXT.get(a.outcome, a.outcome)
    text = f"{a.size_slug} in {a.region}: {what}"
    if a.outcome == "booted" and a.duration_ms:
        text += f" after {a.duration_ms / 1000:.0f} s"
    if a.error_text and a.outcome not in ("created", "booted"):
        text += f" ({a.error_text[:500]})"
    level = "error" if a.outcome in ("error", "boot_failure", "late_failure", "quota") else None
    if a.outcome in ("capacity", "volume_busy"):
        level = "warning"
    return _aivo(_ts(a.at), text, level)


async def _history(app: AppContext, inst_id: uuid.UUID, cur: Cursor) -> list[dict[str, Any]]:
    """New aivo lines (events + placement attempts) after the cursor, in time order; advances it."""
    async with app.sessionmaker() as s:
        evs = (
            (
                await s.execute(
                    select(Event)
                    .where(Event.scope == "instance", Event.ref_id == inst_id, Event.id > cur.event)
                    .order_by(Event.id)
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
        atts = (
            (
                await s.execute(
                    select(PlacementAttempt)
                    .where(PlacementAttempt.instance_id == inst_id, PlacementAttempt.id > cur.attempt)
                    .order_by(PlacementAttempt.id)
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
    out: list[dict[str, Any]] = []
    for ev in evs:
        cur.event = ev.id
        if (line := render_event(ev)) is not None:
            out.append(line)
    for a in atts:
        cur.attempt = a.id
        out.append(render_attempt(a))
    out.sort(key=lambda x: x["ts"])
    return out


def _machine_lines(out: LogsOut) -> list[dict[str, Any]]:
    return [{"ts": ln.ts, "source": ln.source, "level": ln.level, "text": ln.text, "seq": ln.seq} for ln in out.lines]


async def stream(
    app: AppContext, inst_id: uuid.UUID, cursor: Cursor, is_disconnected: Any
) -> AsyncIterator[dict[str, Any]]:
    """Yields SSE messages: ``lines`` (a batch of log lines), ``status`` and finally ``end``."""
    wake = app.bus.subscribe()
    upstream: asyncio.Task[LogsOut] | None = None
    last_status: tuple[str, str] | None = None

    def msg(event: str, data: dict[str, Any]) -> dict[str, Any]:
        return {"event": event, "id": str(cursor), "data": data}

    async def fetch(inst: Instance, since: int) -> LogsOut:
        async with worker_client(app, inst) as c:
            return await c.logs(since, wait=UPSTREAM_WAIT_S, limit=500)

    try:
        while True:
            async with app.sessionmaker() as s:
                inst = (await s.execute(select(Instance).where(Instance.id == inst_id))).scalar_one_or_none()
            if inst is None:
                yield msg("end", {"reason": "unknown machine"})
                return
            hist = await _history(app, inst_id, cursor)
            if hist:
                yield msg("lines", {"lines": hist})

            if inst.state == "destroyed":
                saved = [ln for ln in (inst.boot_log or []) if int(ln.get("seq") or 0) > cursor.machine]
                if saved:
                    cursor.machine = max(int(ln.get("seq") or 0) for ln in saved)
                    yield msg("lines", {"lines": saved, "saved": True})
                if inst.failure_reason and inst.outcome == "failed":
                    yield msg(
                        "lines",
                        {"lines": [_aivo(_ts(inst.state_changed_at), f"Failure: {inst.failure_reason}", "error")]},
                    )
                yield msg("end", {"reason": "destroyed", "saved_log": bool(inst.boot_log)})
                return

            reachable = inst.state in REACHABLE_STATES and bool(inst.public_ipv4 and inst.cert_ca_pem)
            if reachable and upstream is None:
                upstream = asyncio.create_task(fetch(inst, cursor.machine))
            elif not reachable:
                status = ("waiting", f"The machine is {STATE_TEXT.get(inst.state, inst.state).lower()}")
                if inst.state == "destroying":
                    status = ("ending", "The machine is being destroyed")
                if status != last_status:
                    last_status = status
                    yield msg("status", {"phase": status[0], "message": status[1], "state": inst.state})

            wake_task = asyncio.ensure_future(wake.get())
            waiters: set[asyncio.Future[Any]] = {wake_task}
            if upstream is not None:
                waiters.add(upstream)
            done, _ = await asyncio.wait(
                waiters, timeout=2 if upstream is None else UPSTREAM_WAIT_S + 20, return_when=asyncio.FIRST_COMPLETED
            )
            if wake_task not in done:
                wake_task.cancel()
            while not wake.empty():
                wake.get_nowait()

            if upstream is not None and upstream in done:
                task, upstream = upstream, None
                try:
                    out = task.result()
                except (TimeoutError, WorkerUnreachable, OSError, httpx.HTTPError, RuntimeError) as e:
                    status = (
                        "unreachable",
                        "The machine is not answering yet"
                        if inst.state == "booting"
                        else "The machine is not answering",
                    )
                    if status != last_status:
                        last_status = status
                        yield msg(
                            "status",
                            {"phase": status[0], "message": status[1], "state": inst.state, "error": str(e)[:200]},
                        )
                    await asyncio.sleep(3)
                    continue
                if last_status is None or last_status[0] != "live":
                    last_status = ("live", "Live")
                    yield msg("status", {"phase": "live", "message": "Live", "state": inst.state})
                extra: list[dict[str, Any]] = []
                if out.reset:
                    cursor.machine = 0
                    extra.append(
                        _aivo(out.lines[0].ts if out.lines else 0, "The machine's software restarted", "warning")
                    )
                if out.dropped:
                    extra.append(
                        _aivo(out.lines[0].ts if out.lines else 0, f"({out.dropped} older lines were skipped)")
                    )
                lines = _machine_lines(out)
                if lines:
                    cursor.machine = max(cursor.machine, out.next_seq)
                if extra or lines:
                    yield msg("lines", {"lines": extra + lines})
            if await is_disconnected():
                return
    finally:
        app.bus.unsubscribe(wake)
        if upstream is not None:
            upstream.cancel()
            with contextlib.suppress(BaseException):
                await upstream
