"""Key-encryption keys (KEKs) and derived keys.

KEKs come from a secret (env / Kubernetes Secret), formatted as
``kid1:<base64 32 bytes>,kid2:<base64 32 bytes>``. One key id is active for new
data; older ids stay available for decryption until ``aivo rewrap-keys`` runs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class KeyringError(Exception):
    pass


def _hkdf(key: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(key)


@dataclass
class Keyring:
    keys: dict[str, bytes]
    active_id: str

    @classmethod
    def parse(cls, spec: str, active_id: str | None = None) -> Keyring:
        keys: dict[str, bytes] = {}
        for part in (p.strip() for p in spec.split(",")):
            if not part:
                continue
            if ":" not in part:
                raise KeyringError("KEK entries must look like '<key-id>:<base64 key>'")
            kid, b64 = part.split(":", 1)
            kid = kid.strip()
            if not kid or len(kid.encode()) > 64:
                raise KeyringError("key id must be 1..64 bytes")
            try:
                raw = base64.b64decode(b64.strip(), validate=True)
            except ValueError as e:
                raise KeyringError(f"key {kid!r} is not valid base64") from e
            if len(raw) != 32:
                raise KeyringError(f"key {kid!r} must be exactly 32 bytes (got {len(raw)})")
            keys[kid] = raw
        if not keys:
            raise KeyringError("no media encryption keys configured (AIVO_MEDIA_KEKS)")
        active = active_id or next(iter(keys))
        if active not in keys:
            raise KeyringError(f"active key id {active!r} not present in keyring")
        return cls(keys=keys, active_id=active)

    @staticmethod
    def generate_spec(kid: str = "k1") -> str:
        return f"{kid}:{base64.b64encode(os.urandom(32)).decode()}"

    def kek(self, kid: str) -> bytes:
        try:
            return self.keys[kid]
        except KeyError as e:
            raise KeyringError(f"unknown key id {kid!r}") from e

    # -- DEK wrapping -------------------------------------------------------------

    def wrap(self, dek: bytes, aad: bytes, kid: str | None = None) -> tuple[str, bytes, bytes]:
        kid = kid or self.active_id
        nonce = os.urandom(12)
        wrapped = AESGCM(self.kek(kid)).encrypt(nonce, dek, aad)
        return kid, nonce, wrapped

    def unwrap(self, kid: str, nonce: bytes, wrapped: bytes, aad: bytes) -> bytes:
        return AESGCM(self.kek(kid)).decrypt(nonce, wrapped, aad)

    # -- field encryption (prompts, lyrics) ---------------------------------------

    _FIELD_MAGIC = b"F1"

    def seal(self, plaintext: str | None, context: bytes = b"") -> bytes | None:
        if plaintext is None:
            return None
        kid = self.active_id
        key = _hkdf(self.kek(kid), b"aivo-field-v1")
        nonce = os.urandom(12)
        kid_b = kid.encode()
        header = self._FIELD_MAGIC + bytes([len(kid_b)]) + kid_b
        ct = AESGCM(key).encrypt(nonce, plaintext.encode(), header + context)
        return header + nonce + ct

    def open(self, blob: bytes | None, context: bytes = b"") -> str | None:
        if blob is None:
            return None
        blob = bytes(blob)
        if blob[:2] != self._FIELD_MAGIC:
            raise KeyringError("not a sealed field")
        n = blob[2]
        kid = blob[3 : 3 + n].decode()
        header = blob[: 3 + n]
        nonce = blob[3 + n : 15 + n]
        ct = blob[15 + n :]
        key = _hkdf(self.kek(kid), b"aivo-field-v1")
        return AESGCM(key).decrypt(nonce, ct, header + context).decode()

    # -- keyed integrity tag for plaintext content ----------------------------------

    def content_mac(self, digest_sha256: bytes, kid: str | None = None) -> str:
        key = _hkdf(self.kek(kid or self.active_id), b"aivo-content-mac-v1")
        return hmac.new(key, digest_sha256, hashlib.sha256).hexdigest()
