"""Controller leadership via a lease row with a fencing epoch.

Leadership only avoids duplicate work; correctness comes from compare-and-set
state transitions. When renewal fails, the holder must stop all controller work
immediately (the supervisor cancels its tasks).
"""

from __future__ import annotations

import os
import socket
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LEASE_NAME = "controller"


class LeaseManager:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], ttl_s: int = 30, name: str = LEASE_NAME):
        self.sm = sessionmaker
        self.ttl_s = ttl_s
        self.name = name
        self.holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.epoch: int | None = None

    async def try_acquire(self) -> bool:
        async with self.sm() as s, s.begin():
            row = (
                await s.execute(
                    text(
                        """
                        INSERT INTO leases (name, holder, epoch, expires_at, last_tick_at)
                        VALUES (:n, :h, 1, now() + make_interval(secs => :ttl), now())
                        ON CONFLICT (name) DO UPDATE
                          SET holder = EXCLUDED.holder, epoch = leases.epoch + 1,
                              expires_at = EXCLUDED.expires_at, last_tick_at = now()
                          WHERE leases.expires_at < now() OR leases.holder = EXCLUDED.holder
                        RETURNING epoch
                        """
                    ),
                    {"n": self.name, "h": self.holder, "ttl": self.ttl_s},
                )
            ).first()
        self.epoch = row[0] if row else None
        return self.epoch is not None

    async def renew(self) -> bool:
        if self.epoch is None:
            return False
        async with self.sm() as s, s.begin():
            row = (
                await s.execute(
                    text(
                        """
                        UPDATE leases SET expires_at = now() + make_interval(secs => :ttl), last_tick_at = now()
                        WHERE name = :n AND holder = :h AND epoch = :e
                        RETURNING epoch
                        """
                    ),
                    {"n": self.name, "h": self.holder, "e": self.epoch, "ttl": self.ttl_s},
                )
            ).first()
        if row is None:
            self.epoch = None
            return False
        return True

    async def still_valid(self, session: AsyncSession) -> bool:
        """Fencing check before destructive provider calls."""
        if self.epoch is None:
            return False
        row = (
            await session.execute(
                text("SELECT 1 FROM leases WHERE name = :n AND holder = :h AND epoch = :e AND expires_at > now()"),
                {"n": self.name, "h": self.holder, "e": self.epoch},
            )
        ).first()
        return row is not None

    async def release(self) -> None:
        if self.epoch is None:
            return
        async with self.sm() as s, s.begin():
            await s.execute(
                text("UPDATE leases SET expires_at = now() WHERE name = :n AND holder = :h AND epoch = :e"),
                {"n": self.name, "h": self.holder, "e": self.epoch},
            )
        self.epoch = None


async def lease_status(session: AsyncSession, name: str = LEASE_NAME) -> dict[str, object] | None:
    row = (
        await session.execute(
            text(
                "SELECT holder, epoch, expires_at > now() AS alive, "
                "EXTRACT(EPOCH FROM (now() - last_tick_at)) AS tick_age FROM leases WHERE name = :n"
            ),
            {"n": name},
        )
    ).first()
    if row is None:
        return None
    return {"holder": row[0], "epoch": row[1], "alive": bool(row[2]), "last_tick_age_s": float(row[3] or 0)}
