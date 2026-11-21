"""Brake on repeated start-up failures.

When machines keep failing to start (wrong base image, broken worker software, ...), every
retry bills a GPU machine. After ``boot_failures_before_pause`` start-up failures in a row,
new machines are paused until an admin resumes them or a machine starts fine. Machines that
are already running keep working.

Stored as its own ``settings`` row (not part of RuntimeSettings), so saving the settings page
never overwrites it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.config import RuntimeSettings
from aivo.controller.common import utcnow
from aivo.db.models import PlacementAttempt, Setting
from aivo.events.bus import emit

KEY = "provisioning_paused"


async def _row(s: AsyncSession, *, lock: bool = False) -> dict[str, Any]:
    q = select(Setting).where(Setting.key == KEY)
    if lock:
        q = q.with_for_update()
    row = (await s.execute(q)).scalar_one_or_none()
    return dict(row.value) if row and isinstance(row.value, dict) else {}


async def _write(s: AsyncSession, value: dict[str, Any]) -> None:
    await s.execute(
        insert(Setting)
        .values(key=KEY, value=value)
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value})
    )


async def get_pause(s: AsyncSession) -> dict[str, Any] | None:
    """``{"reason", "at", "failures"}`` while new machines are paused, else None."""
    v = await _row(s)
    return v if v.get("paused") else None


async def resume(s: AsyncSession, *, by: str | None = None) -> bool:
    v = await _row(s, lock=True)
    was = bool(v.get("paused"))
    await _write(s, {"paused": False, "resumed_at": utcnow().isoformat(), "resumed_by": by})
    if was:
        await emit(s, scope="system", type="provisioning_resumed", payload={"by": by})
    return was


async def note_boot_failure(s: AsyncSession, rs: RuntimeSettings, provider: str, reason: str) -> bool:
    """Call after recording a ``boot_failure`` attempt. Pauses new machines when the last N start-ups
    (since the last resume) all failed. Returns True if this call paused them."""
    n = rs.boot_failures_before_pause
    if n <= 0:
        return False
    v = await _row(s, lock=True)
    if v.get("paused"):
        return False
    q = (
        select(PlacementAttempt.outcome)
        .where(PlacementAttempt.provider == provider, PlacementAttempt.outcome.in_(("boot_failure", "booted")))
        .order_by(PlacementAttempt.at.desc(), PlacementAttempt.id.desc())
        .limit(n)
    )
    if v.get("resumed_at"):
        q = q.where(PlacementAttempt.at > datetime.fromisoformat(v["resumed_at"]))
    outcomes = list((await s.execute(q)).scalars().all())
    if len(outcomes) < n or any(o != "boot_failure" for o in outcomes):
        return False
    value = {"paused": True, "reason": reason[:500], "at": utcnow().isoformat(), "failures": n}
    await _write(s, value)
    await emit(s, scope="system", type="provisioning_paused", payload={"reason": value["reason"], "failures": n})
    return True


async def note_boot_success(s: AsyncSession) -> None:
    """A machine started fine: whatever was wrong is fixed."""
    v = await _row(s)
    if v.get("paused"):
        await _write(s, {"paused": False, "resumed_at": utcnow().isoformat(), "resumed_by": "auto"})
        await emit(s, scope="system", type="provisioning_resumed", payload={"by": "auto"})
