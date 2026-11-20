"""Event fan-out.

Writers insert an ``events`` row and ``pg_notify`` its id in the same
transaction, so subscribers in *any* process (API vs controller roles) learn
about it after commit. SSE handlers read the rows by id, which also gives
``Last-Event-ID`` replay for free.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.db.models import Event

log = logging.getLogger(__name__)
CHANNEL = "aivo_events"


async def emit(
    session: AsyncSession,
    *,
    scope: str,
    type: str,
    ref_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> Event:
    ev = Event(scope=scope, type=type, ref_id=ref_id, user_id=user_id, payload=payload or {})
    session.add(ev)
    await session.flush()
    await session.execute(text("SELECT pg_notify(:ch, :id)"), {"ch": CHANNEL, "id": str(ev.id)})
    return ev


class EventBus:
    """Listens on Postgres NOTIFY and wakes in-process subscribers."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn: asyncpg.Connection | None = None
        self._subs: set[asyncio.Queue[int]] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="event-bus")

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                self._conn = await asyncpg.connect(self.dsn)
                await self._conn.add_listener(CHANNEL, self._on_notify)
                backoff = 1.0
                while not self._stopping and not self._conn.is_closed():
                    await asyncio.sleep(5)
                    # keepalive; detects dropped connections
                    await self._conn.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # reconnect forever
                log.warning("event bus connection lost: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                if self._conn is not None and not self._conn.is_closed():
                    with contextlib.suppress(Exception):
                        await self._conn.close()

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        try:
            ev_id = int(payload)
        except ValueError:
            return
        for q in list(self._subs):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(ev_id)

    def subscribe(self) -> asyncio.Queue[int]:
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[int]) -> None:
        self._subs.discard(q)

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
