#!/usr/bin/env python3
"""A tiny OpenID Connect provider for LOCAL DEVELOPMENT ONLY (never expose it).

Lets you try aivo's single sign-on without a real identity provider. The sign-in page
lets you type any email/name/groups and issues RS256-signed ID tokens (Authorization
Code + PKCE, client secret).

    uv run python scripts/dev_oidc_provider.py --port 9000
    # then start the backend with:
    AIVO_OIDC_ISSUER=http://localhost:9000 AIVO_OIDC_CLIENT_ID=aivo AIVO_OIDC_CLIENT_SECRET=dev-secret \\
    AIVO_OIDC_NAME="Dev SSO" uv run aivo serve
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import secrets
import time
from urllib.parse import urlencode

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jwt.algorithms import RSAAlgorithm

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=9000)
ap.add_argument("--client-id", default="aivo")
ap.add_argument("--client-secret", default="dev-secret")
args = ap.parse_args()

ISSUER = f"http://localhost:{args.port}"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {**json.loads(RSAAlgorithm.to_jwk(KEY.public_key())), "kid": "dev", "use": "sig", "alg": "RS256"}
CODES: dict[str, dict] = {}
app = FastAPI(title="dev OIDC provider")


@app.get("/.well-known/openid-configuration")
def discovery() -> dict:
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        "userinfo_endpoint": f"{ISSUER}/userinfo",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
    }


@app.get("/jwks")
def jwks() -> dict:
    return {"keys": [JWK]}


@app.get("/authorize", response_class=HTMLResponse)
def authorize_page(request: Request) -> str:
    q = dict(request.query_params)
    if q.get("client_id") != args.client_id:
        raise HTTPException(400, "unknown client_id")
    hidden = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">' for k, v in q.items())
    return f"""<!doctype html><meta name=viewport content="width=device-width">
<title>Dev SSO</title>
<body style="font-family:system-ui;max-width:360px;margin:60px auto">
<h2>Dev SSO (local testing only)</h2>
<form method="post" action="/authorize">{hidden}
<p><label>Email<br><input name="email" value="alice@example.com" style="width:100%"></label></p>
<p><label>Name<br><input name="name" value="Alice" style="width:100%"></label></p>
<p><label>Groups (comma separated)<br><input name="groups" value="aivo-admins" style="width:100%"></label></p>
<p><label><input type="checkbox" name="email_verified" value="true" checked> email verified</label></p>
<button type="submit">Sign in</button>
<button type="submit" name="deny" value="1">Cancel</button>
</form>"""


@app.post("/authorize")
async def authorize_submit(request: Request) -> RedirectResponse:
    f = dict(await request.form())
    redirect_uri, state = f["redirect_uri"], f.get("state", "")
    if f.get("deny"):
        return RedirectResponse(f"{redirect_uri}?{urlencode({'error': 'access_denied', 'state': state})}", 303)
    email = f.get("email", "").strip().lower()
    code = secrets.token_urlsafe(16)
    CODES[code] = {
        "redirect_uri": redirect_uri,
        "challenge": f.get("code_challenge"),
        "nonce": f.get("nonce"),
        "claims": {
            "sub": hashlib.sha256(email.encode()).hexdigest()[:16],
            "email": email,
            "email_verified": f.get("email_verified") == "true",
            "name": f.get("name") or email,
            "groups": [g.strip() for g in f.get("groups", "").split(",") if g.strip()],
        },
    }
    return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", 303)


@app.post("/token")
async def token(
    request: Request, code: str = Form(...), code_verifier: str = Form(""), redirect_uri: str = Form(...)
) -> JSONResponse:
    auth = request.headers.get("authorization", "")
    expected = "Basic " + base64.b64encode(f"{args.client_id}:{args.client_secret}".encode()).decode()
    form = await request.form()
    if auth != expected and form.get("client_secret") != args.client_secret:
        return JSONResponse({"error": "invalid_client"}, 401)
    c = CODES.pop(code, None)
    if not c or c["redirect_uri"] != redirect_uri:
        return JSONResponse({"error": "invalid_grant"}, 400)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    if c["challenge"] and challenge != c["challenge"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, 400)
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": args.client_id, "iat": now, "exp": now + 300, "nonce": c["nonce"], **c["claims"]}
    id_token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "dev"})
    return JSONResponse({"id_token": id_token, "access_token": secrets.token_urlsafe(16), "token_type": "Bearer"})


@app.get("/userinfo")
def userinfo() -> dict:
    return {}


if __name__ == "__main__":
    print(f"dev OIDC provider on {ISSUER} (client_id={args.client_id}, client_secret={args.client_secret})")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
