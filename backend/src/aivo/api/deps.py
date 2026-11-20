from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.context import AppContext
from aivo.db.models import Session as DbSession
from aivo.db.models import User

SESSION_COOKIE = "aivo_session"


def get_app(request: Request) -> AppContext:
    return request.app.state.ctx  # type: ignore[no-any-return]


async def get_session(app: AppContext = Depends(get_app)) -> AsyncIterator[AsyncSession]:
    async with app.sessionmaker() as s:
        yield s


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def current_user(request: Request, s: AsyncSession = Depends(get_session)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Please sign in")
    now = datetime.now(UTC)
    row = (
        await s.execute(
            select(DbSession, User)
            .join(User, User.id == DbSession.user_id)
            .where(DbSession.token_sha256 == token_hash(token), DbSession.expires_at > now, User.is_active.is_(True))
        )
    ).first()
    if row is None:
        raise HTTPException(401, "Please sign in")
    sess, user = row
    if (now - sess.last_seen_at) > timedelta(minutes=5):
        await s.execute(update(DbSession).where(DbSession.id == sess.id).values(last_seen_at=now))
        await s.commit()
    return user  # type: ignore[no-any-return]


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Only admins can do this")
    return user
