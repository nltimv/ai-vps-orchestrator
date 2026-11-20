from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request


def sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenAuth:
    """Bearer-token check (constant time). Only the token hash is kept in memory/disk."""

    def __init__(self, token_sha256: str):
        self._hash = token_sha256
        self.last_contact = time.monotonic()

    def check(self, request: Request) -> None:
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            raise HTTPException(status_code=401, detail="missing token")
        if not hmac.compare_digest(sha256_hex(presented), self._hash):
            raise HTTPException(status_code=401, detail="bad token")
        self.last_contact = time.monotonic()

    def rotate(self) -> str:
        token = secrets.token_urlsafe(32)
        self._hash = sha256_hex(token)
        return token
