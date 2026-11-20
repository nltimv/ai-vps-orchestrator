"""Single sign-on (OpenID Connect) routes.

GET /api/v1/auth/oidc/login     -> redirect to the identity provider
GET /api/v1/auth/oidc/callback  -> verify, find/link/create the user, start a session, redirect into the app

Account rules (see docs/sso.md):
* an SSO identity is the pair (issuer, subject); once linked it always maps to the same user
* first sign-in links to an existing account only if the provider says the email is *verified*
* otherwise: a pending invite (from an invite link) -> the very first user (becomes admin) ->
  auto-provisioning (if enabled) -> refused
* optional allow-lists (email domains, groups) are checked on every sign-in
* if admin groups are configured, the admin role follows group membership on every sign-in
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.api.deps import get_app, get_session, token_hash
from aivo.api.routes.auth import _start_session
from aivo.auth.oidc import Identity, OIDCClient, OIDCError, pkce_pair
from aivo.context import AppContext
from aivo.db.models import AuditLog, Invite, User, UserIdentity

router = APIRouter(prefix="/auth/oidc", tags=["auth"])
log = logging.getLogger(__name__)

STATE_COOKIE = "aivo_oidc"
STATE_TTL_S = 600
COOKIE_PATH = "/api/v1/auth/oidc"
CALLBACK_PATH = "/api/v1/auth/oidc/callback"
_SEAL_CONTEXT = b"aivo-oidc-state-v1"


def client_for(app: AppContext) -> OIDCClient:
    if app.oidc is None:
        app.oidc = OIDCClient(app.env)
    return app.oidc  # type: ignore[no-any-return]


def _safe_return_to(v: str | None) -> str:
    if not v or not v.startswith("/") or v.startswith("//") or v.startswith("/\\") or "\n" in v or "\r" in v:
        return "/"
    return v[:500]


def _redirect_uri(app: AppContext, request: Request) -> str:
    base = app.env.public_url or str(request.base_url)
    return base.rstrip("/") + CALLBACK_PATH


def _fail(code: str) -> RedirectResponse:
    r = RedirectResponse(f"/?sso_error={code}", status_code=303)
    r.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return r


def _seal(app: AppContext, data: dict[str, Any]) -> str:
    blob = app.keyring.seal(json.dumps(data), _SEAL_CONTEXT)
    assert blob is not None
    return base64.urlsafe_b64encode(blob).decode().rstrip("=")


def _unseal(app: AppContext, value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        blob = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        data = json.loads(app.keyring.open(blob, _SEAL_CONTEXT) or "{}")
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < time.time():
        return None
    return data


@router.get("/login")
async def login(
    request: Request, return_to: str | None = None, invite: str | None = None, app: AppContext = Depends(get_app)
) -> RedirectResponse:
    if not app.env.oidc_enabled:
        raise HTTPException(404, "Single sign-on is not configured")
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    redirect_uri = _redirect_uri(app, request)
    try:
        url = await client_for(app).authorization_url(redirect_uri, state, nonce, challenge)
    except OIDCError as e:
        log.warning("SSO login could not start: %s", e)
        return _fail(e.code)
    resp = RedirectResponse(url, status_code=302)
    cookie = _seal(
        app,
        {
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "return_to": _safe_return_to(return_to),
            "invite": invite,
            "exp": time.time() + STATE_TTL_S,
        },
    )
    resp.set_cookie(
        STATE_COOKIE,
        cookie,
        max_age=STATE_TTL_S,
        httponly=True,
        samesite="lax",
        secure=app.env.session_cookie_secure,
        path=COOKIE_PATH,
    )
    return resp


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if not app.env.oidc_enabled:
        raise HTTPException(404, "Single sign-on is not configured")
    saved = _unseal(app, request.cookies.get(STATE_COOKIE))
    if error:
        log.info("SSO provider returned an error: %s", error)
        return _fail("denied" if error == "access_denied" else "provider_error")
    if saved is None:
        return _fail("session_expired")
    if not code or not state or not secrets.compare_digest(state, str(saved.get("state", ""))):
        return _fail("session_expired")
    oidc = client_for(app)
    try:
        tokens = await oidc.exchange_code(code, saved["verifier"], saved["redirect_uri"])
        ident = await oidc.identity(tokens, saved["nonce"])
        user = await resolve_user(app, s, ident, invite_token=saved.get("invite"))
    except OIDCError as e:
        log.warning("SSO sign-in refused: %s", e)
        await s.rollback()
        return _fail(e.code)
    resp = RedirectResponse(saved.get("return_to") or "/", status_code=303)
    await _start_session(app, s, user, request, resp)
    await s.commit()
    resp.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return resp


def _check_access(app: AppContext, ident: Identity) -> None:
    env = app.env
    if env.oidc_allowed_domains:
        domain = ident.email.rsplit("@", 1)[-1] if ident.email else None
        if not domain or ident.email_verified is False or domain not in {d.lower() for d in env.oidc_allowed_domains}:
            raise OIDCError("not_allowed", f"email domain {domain!r} not allowed")
    if env.oidc_allowed_groups:
        allowed = set(env.oidc_allowed_groups) | set(env.oidc_admin_groups)
        if not (ident.groups & allowed):
            raise OIDCError("not_allowed", "not a member of an allowed group")


def _role_from_groups(app: AppContext, ident: Identity) -> str | None:
    if not app.env.oidc_admin_groups:
        return None
    return "admin" if ident.groups & set(app.env.oidc_admin_groups) else "member"


async def resolve_user(app: AppContext, s: AsyncSession, ident: Identity, *, invite_token: str | None) -> User:
    """Find, link or create the account for an SSO identity (inside the caller's transaction)."""
    env = app.env
    _check_access(app, ident)
    # serialize first-user bootstrap / provisioning races
    await s.execute(select(func.pg_advisory_xact_lock(4243)))
    now = datetime.now(UTC)
    group_role = _role_from_groups(app, ident)

    row = (
        await s.execute(
            select(UserIdentity, User)
            .join(User, User.id == UserIdentity.user_id)
            .where(UserIdentity.issuer == ident.issuer, UserIdentity.subject == ident.subject)
        )
    ).first()
    if row is not None:
        link, user = row
        if not user.is_active:
            raise OIDCError("account_disabled")
        link.last_login_at, link.email = now, ident.email
        if group_role:
            user.role = group_role
        if user.password_hash is None:  # SSO-managed account: keep profile in sync
            user.display_name = ident.name or user.display_name
            if ident.email and ident.email != user.email and not await _email_taken(s, ident.email, user.id):
                user.email = ident.email
        s.add(AuditLog(user_id=user.id, action="sso.login", detail={"issuer": ident.issuer}))
        return user

    # first sign-in with this identity: link to an existing account by verified email
    if env.oidc_link_by_email and ident.email and ident.email_verified is True:
        user = (await s.execute(select(User).where(User.email == ident.email))).scalar_one_or_none()
        if user is not None:
            if not user.is_active:
                raise OIDCError("account_disabled")
            if group_role:
                user.role = group_role
            s.add(
                UserIdentity(
                    user_id=user.id, issuer=ident.issuer, subject=ident.subject, email=ident.email, last_login_at=now
                )
            )
            s.add(AuditLog(user_id=user.id, action="sso.link", detail={"issuer": ident.issuer}))
            return user

    if not ident.email:
        raise OIDCError("no_email", "the provider did not share an email address (add the 'email' scope)")
    if await _email_taken(s, ident.email, None):
        raise OIDCError("email_in_use", "an account with this email exists but the email is not verified by SSO")

    role: str | None = None
    invite = None
    if invite_token:
        invite = (
            await s.execute(
                select(Invite)
                .where(
                    Invite.token_sha256 == token_hash(invite_token), Invite.used_at.is_(None), Invite.expires_at > now
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if invite is not None:
            role = invite.role
    if role is None:
        n_users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
        if n_users == 0:
            role = "admin"  # first person to set up aivo
        elif env.oidc_auto_provision:
            role = "member"
        else:
            raise OIDCError("not_invited", "automatic sign-up is off and no invite was used")
    user = User(
        email=ident.email,
        display_name=ident.name or ident.email.split("@")[0],
        password_hash=None,
        role=group_role or role,
    )
    s.add(user)
    await s.flush()
    if invite is not None:
        invite.used_at, invite.used_by = now, user.id
    s.add(
        UserIdentity(user_id=user.id, issuer=ident.issuer, subject=ident.subject, email=ident.email, last_login_at=now)
    )
    s.add(
        AuditLog(
            user_id=user.id,
            action="sso.provision",
            detail={"issuer": ident.issuer, "role": user.role, "via_invite": invite is not None},
        )
    )
    return user


async def _email_taken(s: AsyncSession, email: str, except_user: Any) -> bool:
    q = select(User.id).where(User.email == email)
    if except_user is not None:
        q = q.where(User.id != except_user)
    return (await s.execute(q.limit(1))).first() is not None
