"""OpenID Connect (single sign-on) client.

Authorization Code flow with PKCE (S256), ``state`` and ``nonce``. The ID token is
verified against the provider's published keys (JWKS): signature, issuer, audience
(and ``azp``), expiry and nonce. Works with confidential clients (client secret) and
public clients (PKCE only), e.g. Keycloak, Authentik, Authelia, Zitadel, Okta,
Microsoft Entra ID, Google.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from aivo.config import EnvSettings

ALLOWED_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
CACHE_TTL_S = 3600
LEEWAY_S = 60


class OIDCError(Exception):
    """Sign-in failed. ``code`` is a short, user-facing reason key."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool | None
    name: str | None
    groups: frozenset[str]
    claims: dict[str, Any] = field(default_factory=dict, compare=False)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _norm(issuer: str) -> str:
    return issuer.rstrip("/")


class OIDCClient:
    def __init__(self, env: EnvSettings, *, http: httpx.AsyncClient | None = None):
        if not env.oidc_enabled:
            raise OIDCError("not_configured")
        assert env.oidc_issuer and env.oidc_client_id
        self.issuer = env.oidc_issuer
        self.client_id = env.oidc_client_id
        self.client_secret = env.oidc_client_secret.get_secret_value() if env.oidc_client_secret else None
        self.scopes = env.oidc_scopes if "openid" in env.oidc_scopes.split() else f"openid {env.oidc_scopes}"
        self.groups_claim = env.oidc_groups_claim
        self._http = http
        self._meta: tuple[float, dict[str, Any]] | None = None
        self._jwks: tuple[float, dict[str, Any]] | None = None

    def _client(self) -> httpx.AsyncClient:
        return self._http or httpx.AsyncClient(timeout=15, follow_redirects=True)

    async def _get_json(self, url: str) -> dict[str, Any]:
        own = self._http is None
        c = self._client()
        try:
            r = await c.get(url, headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            raise OIDCError("provider_unreachable", f"{url}: {e}") from e
        finally:
            if own:
                await c.aclose()

    async def metadata(self) -> dict[str, Any]:
        if self._meta and time.monotonic() - self._meta[0] < CACHE_TTL_S:
            return self._meta[1]
        meta = await self._get_json(_norm(self.issuer) + "/.well-known/openid-configuration")
        if _norm(str(meta.get("issuer", ""))) != _norm(self.issuer):
            raise OIDCError("misconfigured", f"discovery issuer {meta.get('issuer')!r} != configured {self.issuer!r}")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not meta.get(k):
                raise OIDCError("misconfigured", f"discovery document lacks {k}")
        self._meta = (time.monotonic(), meta)
        return meta

    async def _jwks_doc(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self._jwks and time.monotonic() - self._jwks[0] < CACHE_TTL_S:
            return self._jwks[1]
        meta = await self.metadata()
        doc = await self._get_json(meta["jwks_uri"])
        self._jwks = (time.monotonic(), doc)
        return doc

    async def authorization_url(self, redirect_uri: str, state: str, nonce: str, code_challenge: str) -> str:
        meta = await self.metadata()
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        sep = "&" if "?" in meta["authorization_endpoint"] else "?"
        return meta["authorization_endpoint"] + sep + urlencode(params)

    async def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
        meta = await self.metadata()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
        }
        auth = None
        if self.client_secret:
            methods = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
            if "client_secret_basic" in methods:
                auth = httpx.BasicAuth(self.client_id, self.client_secret)
            else:
                data["client_secret"] = self.client_secret
        own = self._http is None
        c = self._client()
        try:
            r = await c.post(meta["token_endpoint"], data=data, auth=auth, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise OIDCError("provider_unreachable", str(e)) from e
        finally:
            if own:
                await c.aclose()
        if r.status_code != 200:
            raise OIDCError("token_exchange_failed", f"HTTP {r.status_code}: {r.text[:300]}")
        tokens = r.json()
        if "id_token" not in tokens:
            raise OIDCError("token_exchange_failed", "no id_token in response (is the 'openid' scope granted?)")
        return tokens  # type: ignore[no-any-return]

    async def _key_for(self, token: str) -> tuple[Any, str]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise OIDCError("invalid_token", f"bad header: {e}") from e
        alg = header.get("alg")
        if alg not in ALLOWED_ALGS:
            raise OIDCError("invalid_token", f"unsupported signing algorithm {alg!r}")
        kid = header.get("kid")
        for force in (False, True):  # refetch once on an unknown key id (key rotation)
            keys = (await self._jwks_doc(force=force)).get("keys", [])
            for k in keys:
                if (kid is None or k.get("kid") == kid) and k.get("use", "sig") == "sig":
                    try:
                        return jwt.PyJWK(k, algorithm=alg).key, alg
                    except jwt.PyJWTError:
                        continue
        raise OIDCError("invalid_token", f"no signing key {kid!r} published by the provider")

    async def verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        key, alg = await self._key_for(id_token)
        meta = await self.metadata()
        try:
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=[alg],
                audience=self.client_id,
                issuer=meta["issuer"],
                leeway=LEEWAY_S,
                options={"require": ["iss", "aud", "exp", "iat", "sub"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise OIDCError("invalid_token", "token expired") from e
        except jwt.PyJWTError as e:
            raise OIDCError("invalid_token", str(e)) from e
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.client_id:
            raise OIDCError("invalid_token", "azp does not match client id")
        if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OIDCError("invalid_token", "nonce mismatch")
        return claims  # type: ignore[no-any-return]

    async def userinfo(self, access_token: str) -> dict[str, Any]:
        meta = await self.metadata()
        url = meta.get("userinfo_endpoint")
        if not url or not access_token:
            return {}
        own = self._http is None
        c = self._client()
        try:
            r = await c.get(url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
            return r.json() if r.status_code == 200 else {}  # type: ignore[no-any-return]
        except (httpx.HTTPError, json.JSONDecodeError):
            return {}
        finally:
            if own:
                await c.aclose()

    async def identity(self, tokens: dict[str, Any], nonce: str) -> Identity:
        claims = await self.verify_id_token(tokens["id_token"], nonce)
        merged = dict(claims)
        if not claims.get("email") or self.groups_claim not in claims:
            info = await self.userinfo(tokens.get("access_token", ""))
            if info.get("sub") and info["sub"] != claims["sub"]:
                raise OIDCError("invalid_token", "userinfo subject mismatch")
            for k, v in info.items():
                merged.setdefault(k, v)
        email = merged.get("email")
        if not email:
            pu = merged.get("preferred_username") or merged.get("upn")
            email = pu if isinstance(pu, str) and "@" in pu else None
        ev = merged.get("email_verified")
        if isinstance(ev, str):
            ev = ev.lower() == "true"
        name = merged.get("name") or merged.get("preferred_username") or (email.split("@")[0] if email else None)
        return Identity(
            issuer=_norm(str(claims["iss"])),
            subject=str(claims["sub"]),
            email=email.lower() if isinstance(email, str) else None,
            email_verified=ev if isinstance(ev, bool) else None,
            name=str(name)[:120] if name else None,
            groups=_groups(merged.get(self.groups_claim)),
            claims=merged,
        )


def _groups(v: Any) -> frozenset[str]:
    if v is None:
        return frozenset()
    if isinstance(v, str):
        return frozenset(x for x in v.replace(",", " ").split() if x)
    if isinstance(v, list | tuple | set):
        return frozenset(str(x).strip("/") for x in v) | frozenset(str(x) for x in v)
    return frozenset()
