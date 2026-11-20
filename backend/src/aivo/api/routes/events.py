"""Server-Sent Events: live job progress and machine status for the dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, or_, select
from sse_starlette.sse import EventSourceResponse

from aivo.api.deps import current_user, get_app
from aivo.context import AppContext
from aivo.db.models import Event, User

router = APIRouter(tags=["events"])


@router.get("/events")
async def events(request: Request, since: int | None = None, user: User = Depends(current_user),
                 app: AppContext = Depends(get_app)) -> EventSourceResponse:
    last_id = since
    hdr = request.headers.get("last-event-id")
    if hdr and hdr.isdigit():
        last_id = int(hdr)

    def visible(q):  # type: ignore[no-untyped-def]
        if user.role == "admin":
            return q
        # members: their own jobs, machine status (no secrets in payloads), gallery-wide successes
        return q.where(or_(Event.user_id == user.id, Event.scope == "instance",
                           (Event.scope == "job") & (Event.type == "job.state")))

    async def gen() -> AsyncIterator[dict[str, str]]:
        nonlocal last_id
        q_wake = app.bus.subscribe()
        try:
            if last_id is None:
                async with app.sessionmaker() as s:
                    last_id = (await s.execute(select(func.coalesce(func.max(Event.id), 0)))).scalar_one()
            yield {"event": "hello", "data": json.dumps({"last_id": last_id}), "id": str(last_id)}
            while True:
                async with app.sessionmaker() as s:
                    rows = (await s.execute(
                        visible(select(Event).where(Event.id > last_id)).order_by(Event.id).limit(200)
                    )).scalars().all()
                for ev in rows:
                    last_id = ev.id
                    yield {"event": ev.type, "id": str(ev.id), "data": json.dumps({
                        "scope": ev.scope, "ref_id": str(ev.ref_id) if ev.ref_id else None,
                        "user_id": str(ev.user_id) if ev.user_id else None, "payload": ev.payload,
                        "at": ev.at.isoformat(),
                    })}
                if await request.is_disconnected():
                    return
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(q_wake.get(), timeout=15)
                    while not q_wake.empty():
                        q_wake.get_nowait()
        finally:
            app.bus.unsubscribe(q_wake)

    return EventSourceResponse(gen(), ping=15)
