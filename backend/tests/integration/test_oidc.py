"""Single sign-on: full Authorization Code + PKCE flow against a fake OpenID provider."""

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from tests.integration.harness import Harness

ISSUER = "https://idp.test/realms/home"
CLIENT_ID = "aivo"
CLIENT_SECRET = "s3cret"


class FakeIdP:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid="k1", use="sig", alg="RS256")
        self.jwks = {"keys": [jwk]}
        self.codes: dict[str, dict] = {}
        self.tamper: dict = {}  # override id_token claims, e.g. {"nonce": "x"} / {"aud": "other"}

    def install(self, m: respx.MockRouter) -> None:
        m.get(f"{ISSUER}/.well-known/openid-configuration").respond(
            json={
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                "userinfo_endpoint": f"{ISSUER}/protocol/openid-connect/userinfo",
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            }
        )
        m.get(f"{ISSUER}/protocol/openid-connect/certs").respond(json=self.jwks)
        m.post(f"{ISSUER}/protocol/openid-connect/token").mock(side_effect=self._token)
        m.get(f"{ISSUER}/protocol/openid-connect/userinfo").respond(json={})

    def authorize(self, location: str, claims: dict) -> tuple[str, str]:
        q = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
        assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID
        assert q["code_challenge_method"] == "S256" and "openid" in q["scope"].split()
        code = secrets.token_urlsafe(8)
        self.codes[code] = {
            "nonce": q["nonce"],
            "challenge": q["code_challenge"],
            "redirect_uri": q["redirect_uri"],
            "claims": claims,
        }
        return code, q["state"]

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        auth = request.headers.get("authorization", "")
        if auth != "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode():
            return httpx.Response(401, json={"error": "invalid_client"})
        c = self.codes.pop(form.get("code", ""), None)
        if c is None or form.get("redirect_uri") != c["redirect_uri"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        verifier = form.get("code_verifier", "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if challenge != c["challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": c["nonce"],
            **c["claims"],
            **self.tamper,
        }
        tok = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})
        return httpx.Response(200, json={"id_token": tok, "access_token": "at", "token_type": "Bearer"})


def oidc_env(env, **kw):
    return env.model_copy(
        update={
            "oidc_issuer": ISSUER,
            "oidc_client_id": CLIENT_ID,
            "oidc_client_secret": __import__("pydantic").SecretStr(CLIENT_SECRET),
            "oidc_name": "Home SSO",
            **kw,
        }
    )


def person(sub: str, email: str, verified: bool | None = True, groups: list[str] | None = None, name: str = "") -> dict:
    c = {"sub": sub, "email": email, "name": name or email.split("@")[0]}
    if verified is not None:
        c["email_verified"] = verified
    if groups is not None:
        c["groups"] = groups
    return c


async def sso(h: Harness, idp: FakeIdP, claims: dict, *, return_to: str = "/gallery", invite: str | None = None):
    params = {"return_to": return_to}
    if invite:
        params["invite"] = invite
    r = await h.client.get("/api/v1/auth/oidc/login", params=params, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"].startswith(f"{ISSUER}/protocol/openid-connect/auth?")
    code, state = idp.authorize(r.headers["location"], claims)
    return await h.client.get(
        "/api/v1/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False
    )


def error_of(r: httpx.Response) -> str | None:
    loc = r.headers.get("location", "")
    return parse_qs(urlparse(loc).query).get("sso_error", [None])[0]


@pytest.fixture
def idp():
    fake = FakeIdP()
    with respx.mock(assert_all_called=False) as m:
        fake.install(m)
        yield fake


async def me(h: Harness) -> dict:
    r = await h.client.get("/api/v1/auth/me")
    return r.json() if r.status_code == 200 else {}


async def logout(h: Harness) -> None:
    await h.client.post("/api/v1/auth/logout")
    h.client.cookies.clear()


async def test_first_user_admin_then_members_and_status(env, idp):
    async with Harness(oidc_env(env), role="api") as h:
        st = (await h.client.get("/api/v1/auth/status")).json()
        assert st == {"needs_setup": True, "password_login": True, "sso": {"enabled": True, "name": "Home SSO"}}
        r = await sso(h, idp, person("u1", "Alice@Example.com", name="Alice"))
        assert r.status_code == 303 and r.headers["location"] == "/gallery", r.headers
        a = await me(h)
        assert a["role"] == "admin" and a["email"] == "alice@example.com" and a["display_name"] == "Alice"
        users = (await h.client.get("/api/v1/admin/users")).json()
        assert users[0]["sso"] is True and users[0]["has_password"] is False
        await logout(h)
        await sso(h, idp, person("u2", "bob@example.com"))
        assert (await me(h))["role"] == "member"
        await logout(h)
        # same identity again -> same account
        await sso(h, idp, person("u1", "alice@example.com"))
        assert (await me(h))["id"] == a["id"]
        # SSO-only accounts can't sign in with a password
        r = await h.client.post("/api/v1/auth/login", json={"email": "alice@example.com", "password": "anything!!"})
        assert r.status_code == 401


async def test_admin_role_follows_groups(env, idp):
    async with Harness(oidc_env(env, oidc_admin_groups=["aivo-admins"]), role="api") as h:
        await sso(h, idp, person("u1", "a@example.com", groups=["/aivo-admins", "staff"]))  # Keycloak-style path
        assert (await me(h))["role"] == "admin"
        await logout(h)
        await sso(h, idp, person("u1", "a@example.com", groups=["staff"]))
        assert (await me(h))["role"] == "member"


async def test_links_existing_account_only_with_verified_email(env, idp):
    async with Harness(oidc_env(env), role="api") as h:
        owner = await h.setup_admin()  # password account admin@example.com
        await logout(h)
        r = await sso(h, idp, person("x1", "admin@example.com", verified=False))
        assert error_of(r) == "email_in_use"
        r = await sso(h, idp, person("x1", "admin@example.com", verified=None))
        assert error_of(r) == "email_in_use"
        r = await sso(h, idp, person("x1", "admin@example.com", verified=True))
        assert r.status_code == 303 and error_of(r) is None
        m = await me(h)
        assert m["id"] == owner["id"] and m["role"] == "admin"
        users = (await h.client.get("/api/v1/admin/users")).json()
        assert users[0]["sso"] is True and users[0]["has_password"] is True


async def test_allow_lists(env, idp):
    async with Harness(
        oidc_env(env, oidc_allowed_domains=["example.com"], oidc_allowed_groups=["aivo"]), role="api"
    ) as h:
        assert error_of(await sso(h, idp, person("a", "a@evil.com", groups=["aivo"]))) == "not_allowed"
        assert error_of(await sso(h, idp, person("b", "b@example.com", groups=["other"]))) == "not_allowed"
        assert (
            error_of(await sso(h, idp, person("c", "c@example.com", verified=False, groups=["aivo"]))) == "not_allowed"
        )
        r = await sso(h, idp, person("d", "d@example.com", groups=["aivo"]))
        assert error_of(r) is None and (await me(h))["email"] == "d@example.com"


async def test_invite_only_mode(env, idp):
    async with Harness(oidc_env(env, oidc_auto_provision=False), role="api") as h:
        await sso(h, idp, person("boss", "boss@example.com"))  # very first user is still allowed (admin)
        assert (await me(h))["role"] == "admin"
        inv = (await h.client.post("/api/v1/auth/invites", json={"role": "member"})).json()
        await logout(h)
        assert error_of(await sso(h, idp, person("n1", "new@example.com"))) == "not_invited"
        r = await sso(h, idp, person("n1", "new@example.com"), invite=inv["token"])
        assert error_of(r) is None and (await me(h))["role"] == "member"
        await logout(h)
        # invite is single-use
        assert error_of(await sso(h, idp, person("n2", "new2@example.com"), invite=inv["token"])) == "not_invited"


async def test_disabled_account_refused(env, idp):
    async with Harness(oidc_env(env), role="api") as h:
        await sso(h, idp, person("boss", "boss@example.com"))
        await logout(h)
        await sso(h, idp, person("m", "m@example.com"))
        m = await me(h)
        await logout(h)
        await sso(h, idp, person("boss", "boss@example.com"))
        await h.client.patch(f"/api/v1/admin/users/{m['id']}", json={"is_active": False})
        await logout(h)
        assert error_of(await sso(h, idp, person("m", "m@example.com"))) == "account_disabled"


async def test_protocol_security_checks(env, idp):
    async with Harness(oidc_env(env), role="api") as h:
        # tampered id_token claims
        for bad, code in [
            ({"nonce": "wrong"}, "invalid_token"),
            ({"aud": "someone-else"}, "invalid_token"),
            ({"iss": "https://evil.test"}, "invalid_token"),
            ({"exp": int(time.time()) - 3600}, "invalid_token"),
        ]:
            idp.tamper = bad
            assert error_of(await sso(h, idp, person("u", "u@example.com"))) == code, bad
        idp.tamper = {}
        # state mismatch / missing state cookie
        r = await h.client.get("/api/v1/auth/oidc/login", follow_redirects=False)
        code, _state = idp.authorize(r.headers["location"], person("u", "u@example.com"))
        r = await h.client.get(
            "/api/v1/auth/oidc/callback", params={"code": code, "state": "forged"}, follow_redirects=False
        )
        assert error_of(r) == "session_expired"
        r = await h.client.get("/api/v1/auth/oidc/login", follow_redirects=False)
        code, state = idp.authorize(r.headers["location"], person("u", "u@example.com"))
        h.client.cookies.clear()
        r = await h.client.get(
            "/api/v1/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False
        )
        assert error_of(r) == "session_expired"
        # PKCE: a code obtained by someone else can't be redeemed with our verifier
        r = await h.client.get("/api/v1/auth/oidc/login", follow_redirects=False)
        code, state = idp.authorize(r.headers["location"], person("u", "u@example.com"))
        idp.codes[code]["challenge"] = "not-our-challenge"
        r = await h.client.get(
            "/api/v1/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False
        )
        assert error_of(r) == "token_exchange_failed"
        # provider-side refusal
        r = await h.client.get("/api/v1/auth/oidc/callback", params={"error": "access_denied"}, follow_redirects=False)
        assert error_of(r) == "denied"
        # open-redirect attempts fall back to "/"
        r = await sso(h, idp, person("u", "u@example.com"), return_to="//evil.com/x")
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert (await me(h))["email"] == "u@example.com"


async def test_password_login_can_be_disabled(env, idp):
    async with Harness(oidc_env(env, password_login=False), role="api") as h:
        st = (await h.client.get("/api/v1/auth/status")).json()
        assert st["password_login"] is False
        r = await h.client.post(
            "/api/v1/auth/setup",
            json={"email": "admin@example.com", "display_name": "A", "password": "correct horse battery"},
        )
        assert r.status_code == 403
        r = await h.client.post("/api/v1/auth/login", json={"email": "a@b.c", "password": "x"})
        assert r.status_code == 403
        await sso(h, idp, person("u1", "first@example.com"))
        assert (await me(h))["role"] == "admin"


async def test_sso_not_configured_is_404(env):
    async with Harness(env, role="api") as h:
        assert (await h.client.get("/api/v1/auth/status")).json()["sso"]["enabled"] is False
        r = await h.client.get("/api/v1/auth/oidc/login", follow_redirects=False)
        assert r.status_code == 404
