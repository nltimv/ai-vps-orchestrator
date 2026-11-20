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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from aivo.db.models import Event
from aivo.db.session import normalize_db_url

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
    """Listens on Postgres NOTIFY and wakes in-process subscribers.

    The listener connection is opened through SQLAlchemy (a dedicated, unpooled engine on the
    same normalized URL), so its connection options (SSL, ports, query parameters) are always
    exactly those of the main engine.
    """

    def __init__(self, url: str, *, initial_backoff_s: float = 1.0, max_backoff_s: float = 30.0):
        self.url = normalize_db_url(url)
        self.initial_backoff_s = initial_backoff_s
        self.max_backoff_s = max_backoff_s
        self.connected = False
        self.last_error: str | None = None
        self._engine: AsyncEngine | None = None
        self._sa_conn: AsyncConnection | None = None
        self._conn: Any = None  # asyncpg.Connection
        self._subs: set[asyncio.Queue[int]] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        self._engine = create_async_engine(self.url, poolclass=NullPool)
        self._task = asyncio.create_task(self._run(), name="event-bus")

    async def _connect(self) -> Any:
        assert self._engine is not None
        self._sa_conn = await self._engine.connect()
        raw = await self._sa_conn.get_raw_connection()
        return raw.driver_connection

    async def _close(self) -> None:
        self.connected = False
        if self._sa_conn is not None:
            with contextlib.suppress(Exception):
                await self._sa_conn.close()
        self._sa_conn, self._conn = None, None

    async def _run(self) -> None:
        backoff = self.initial_backoff_s
        while not self._stopping:
            try:
                self._conn = await self._connect()
                await self._conn.add_listener(CHANNEL, self._on_notify)
                if self.last_error is not None:
                    log.info("live updates restored (database listener reconnected)")
                self.connected, self.last_error = True, None
                backoff = self.initial_backoff_s
                while not self._stopping and not self._conn.is_closed():
                    await asyncio.sleep(5)
                    # keepalive; detects dropped connections
                    await self._conn.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # reconnect forever, but don't flood the log
                msg = str(e) or type(e).__name__
                if msg != self.last_error:
                    log.warning(
                        "live updates paused (database listener): %s; retrying in the background. "
                        "Creations still work; the dashboard refreshes periodically meanwhile.",
                        msg,
                    )
                else:
                    log.debug("database listener still unavailable: %s", msg)
                self.last_error = msg
                await self._close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff_s)
            finally:
                await self._close()

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
        await self._close()
        if self._engine is not None:
            await self._engine.dispose()
