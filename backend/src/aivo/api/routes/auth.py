from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.api.deps import SESSION_COOKIE, current_user, get_app, get_session, require_admin, token_hash
from aivo.api.schemas import AcceptInviteIn, InviteIn, InviteOut, LoginIn, SetupIn, UserOut
from aivo.auth.passwords import hash_password, needs_rehash, verify_password
from aivo.context import AppContext
from aivo.db.models import AuditLog, Invite, User, UserIdentity
from aivo.db.models import Session as DbSession
from aivo.safety import adult_ok

router = APIRouter(prefix="/auth", tags=["auth"])
admin_router = APIRouter(prefix="/admin/users", tags=["admin"])

_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


def _user_out(u: User, adult_content: bool = False) -> UserOut:
    return UserOut(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        role=u.role,  # type: ignore[arg-type]
        adult_allowed=bool(u.adult_allowed),
        adult_content=adult_content,
    )


async def _start_session(app: AppContext, s: AsyncSession, user: User, request: Request, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    s.add(
        DbSession(
            token_sha256=token_hash(token),
            user_id=user.id,
            expires_at=datetime.now(UTC) + timedelta(hours=app.env.session_ttl_hours),
            user_agent=(request.headers.get("user-agent") or "")[:300],
        )
    )
    user.last_login_at = datetime.now(UTC)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=app.env.session_cookie_secure,
        max_age=app.env.session_ttl_hours * 3600,
        path="/",
    )


@router.get("/status")
async def status(app: AppContext = Depends(get_app), s: AsyncSession = Depends(get_session)) -> dict[str, object]:
    """What the sign-in page should offer (no authentication required)."""
    n = (await s.execute(select(func.count()).select_from(User))).scalar_one()
    return {
        "needs_setup": n == 0,
        "password_login": app.env.password_login,
        "sso": {"enabled": app.env.oidc_enabled, "name": app.env.oidc_name},
    }


def _require_password_login(app: AppContext) -> None:
    if not app.env.password_login:
        raise HTTPException(403, "Signing in with a password is turned off. Use single sign-on instead.")


@router.post("/setup")
async def setup(
    body: SetupIn,
    request: Request,
    response: Response,
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> UserOut:
    """First-run: create the initial admin. Only works while there are no users at all."""
    _require_password_login(app)
    await s.execute(select(func.pg_advisory_xact_lock(4242)))
    n = (await s.execute(select(func.count()).select_from(User))).scalar_one()
    if n > 0:
        raise HTTPException(403, "Setup has already been completed")
    user = User(
        email=body.email.lower(),
        display_name=body.display_name,
        password_hash=hash_password(body.password),
        role="admin",
    )
    s.add(user)
    await s.flush()
    await _start_session(app, s, user, request, response)
    s.add(AuditLog(user_id=user.id, action="setup"))
    await s.commit()
    return _user_out(user)


@router.post("/login")
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> UserOut:
    _require_password_login(app)
    user = (await s.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none()
    if user is None or not user.is_active or user.password_hash is None:  # SSO-only accounts have no password
        verify_password(_DUMMY_HASH, body.password)
        raise HTTPException(401, "Wrong email or password")
    if not verify_password(user.password_hash, body.password):
        raise HTTPException(401, "Wrong email or password")
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    await _start_session(app, s, user, request, response)
    await s.commit()
    return _user_out(user)


@router.post("/logout")
async def logout(request: Request, response: Response, s: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await s.execute(
            update(DbSession).where(DbSession.token_sha256 == token_hash(token)).values(expires_at=datetime.now(UTC))
        )
        await s.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(current_user), app: AppContext = Depends(get_app)) -> UserOut:
    return _user_out(user, adult_ok(user, await app.runtime()))


@router.post("/invites")
async def create_invite(
    body: InviteIn, admin: User = Depends(require_admin), s: AsyncSession = Depends(get_session)
) -> InviteOut:
    token = secrets.token_urlsafe(24)
    exp = datetime.now(UTC) + timedelta(days=body.valid_days)
    s.add(Invite(token_sha256=token_hash(token), role=body.role, created_by=admin.id, expires_at=exp))
    s.add(AuditLog(user_id=admin.id, action="invite.create", detail={"role": body.role}))
    await s.commit()
    return InviteOut(token=token, expires_at=exp)


@router.post("/accept-invite")
async def accept_invite(
    body: AcceptInviteIn,
    request: Request,
    response: Response,
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> UserOut:
    _require_password_login(app)
    inv = (
        await s.execute(
            select(Invite)
            .where(
                Invite.token_sha256 == token_hash(body.token),
                Invite.used_at.is_(None),
                Invite.expires_at > datetime.now(UTC),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if inv is None:
        raise HTTPException(400, "This invite link is invalid or has expired")
    if (await s.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none():
        raise HTTPException(409, "An account with this email already exists")
    user = User(
        email=body.email.lower(),
        display_name=body.display_name,
        password_hash=hash_password(body.password),
        role=inv.role,
    )
    s.add(user)
    await s.flush()
    inv.used_at, inv.used_by = datetime.now(UTC), user.id
    await _start_session(app, s, user, request, response)
    await s.commit()
    return _user_out(user)


@admin_router.get("")
async def list_users(_: User = Depends(require_admin), s: AsyncSession = Depends(get_session)) -> list[dict]:  # type: ignore[type-arg]
    users = (await s.execute(select(User).order_by(User.created_at))).scalars().all()
    sso_ids = set((await s.execute(select(UserIdentity.user_id))).scalars().all())
    return [
        {
            **_user_out(u).model_dump(mode="json"),
            "is_active": u.is_active,
            "last_login_at": u.last_login_at,
            "sso": u.id in sso_ids,
            "has_password": u.password_hash is not None,
        }
        for u in users
    ]


@admin_router.patch("/{user_id}")
async def update_user(
    user_id: str,
    body: dict,
    admin: User = Depends(require_admin),  # type: ignore[type-arg]
    s: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    u = (await s.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if u is None:
        raise HTTPException(404, "No such user")
    if u.id == admin.id and (body.get("is_active") is False or body.get("role") == "member"):
        raise HTTPException(400, "You can't demote or deactivate yourself")
    if "is_active" in body:
        u.is_active = bool(body["is_active"])
    if body.get("role") in ("admin", "member"):
        u.role = body["role"]
    if "adult_allowed" in body:
        u.adult_allowed = bool(body["adult_allowed"])
    s.add(AuditLog(user_id=admin.id, action="user.update", target=str(u.id), detail=body))
    await s.commit()
    return {"ok": True}
