"""Chunked envelope encryption for media files (``AIVOENC1``).

Layout::

    stable header (36 bytes, used as AAD everywhere)
      magic "AIVOENC1" | version u8 | alg u8 | chunk_log2 u8 | reserved u8
      file_id (16 bytes, the asset UUID) | plaintext_size u64be
    wrap section (125 bytes, rewritable in place for key rotation)
      kid_len u8 | kid (64 bytes, zero padded) | wrap_nonce (12) | wrapped DEK (32 + 16 tag)
    chunks
      AES-256-GCM(DEK, nonce = chunk_index (11 bytes be) || final_flag (1 byte), aad = stable header)

Each file has its own random DEK, so the deterministic chunk nonces never repeat
under one key. The final flag, the authenticated plaintext size and the exact
expected file length detect truncation, reordering, appending, and a header
copied from another file (file_id is checked against the asset id).
Ranges decrypt only the chunks they cover, which makes video seeking cheap.
"""

from __future__ import annotations

import hashlib
import os
import struct
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aivo.storage.keyring import Keyring

MAGIC = b"AIVOENC1"
VERSION = 1
ALG_AES256_GCM_CHUNKED = 1
DEFAULT_CHUNK_LOG2 = 16
TAG_LEN = 16
KID_FIELD = 64

_STABLE = struct.Struct(">8sBBBB16sQ")  # 36 bytes
STABLE_LEN = _STABLE.size
WRAP_LEN = 1 + KID_FIELD + 12 + 32 + TAG_LEN  # 125
HEADER_LEN = STABLE_LEN + WRAP_LEN


class EnvelopeError(Exception):
    """Raised for any malformed, tampered or mismatched ciphertext."""


@dataclass(frozen=True)
class EnvelopeHeader:
    version: int
    chunk_log2: int
    file_id: uuid.UUID
    plaintext_size: int
    kid: str
    wrap_nonce: bytes
    wrapped_dek: bytes
    stable: bytes

    @property
    def chunk_size(self) -> int:
        return 1 << self.chunk_log2

    @property
    def n_chunks(self) -> int:
        return max(1, -(-self.plaintext_size // self.chunk_size))

    @property
    def ciphertext_size(self) -> int:
        return HEADER_LEN + self.plaintext_size + self.n_chunks * TAG_LEN

    def chunk_offset(self, index: int) -> int:
        return HEADER_LEN + index * (self.chunk_size + TAG_LEN)

    def chunk_plain_len(self, index: int) -> int:
        if index == self.n_chunks - 1:
            return self.plaintext_size - index * self.chunk_size
        return self.chunk_size


@dataclass(frozen=True)
class EncryptResult:
    ciphertext_size: int
    plaintext_size: int
    sha256: bytes


def _nonce(index: int, final: bool) -> bytes:
    return index.to_bytes(11, "big") + (b"\x01" if final else b"\x00")


def _stable_header(file_id: uuid.UUID, size: int, chunk_log2: int) -> bytes:
    return _STABLE.pack(MAGIC, VERSION, ALG_AES256_GCM_CHUNKED, chunk_log2, 0, file_id.bytes, size)


def _wrap_section(kid: str, nonce: bytes, wrapped: bytes) -> bytes:
    kid_b = kid.encode()
    if len(kid_b) > KID_FIELD:
        raise EnvelopeError("key id too long")
    if len(nonce) != 12 or len(wrapped) != 32 + TAG_LEN:
        raise EnvelopeError("bad wrap material")
    return bytes([len(kid_b)]) + kid_b.ljust(KID_FIELD, b"\x00") + nonce + wrapped


def _parse_header(raw: bytes) -> EnvelopeHeader:
    if len(raw) < HEADER_LEN:
        raise EnvelopeError("truncated header")
    magic, version, alg, chunk_log2, _reserved, fid, size = _STABLE.unpack(raw[:STABLE_LEN])
    if magic != MAGIC:
        raise EnvelopeError("not an aivo encrypted file")
    if version != VERSION or alg != ALG_AES256_GCM_CHUNKED:
        raise EnvelopeError(f"unsupported envelope version/alg {version}/{alg}")
    if not 10 <= chunk_log2 <= 24:
        raise EnvelopeError("bad chunk size")
    w = raw[STABLE_LEN:HEADER_LEN]
    kid_len = w[0]
    if kid_len == 0 or kid_len > KID_FIELD:
        raise EnvelopeError("bad key id length")
    kid = w[1 : 1 + kid_len].decode()
    if any(w[1 + kid_len : 1 + KID_FIELD]):
        raise EnvelopeError("non-zero key id padding")
    nonce = w[1 + KID_FIELD : 1 + KID_FIELD + 12]
    wrapped = w[1 + KID_FIELD + 12 :]
    return EnvelopeHeader(
        version=version,
        chunk_log2=chunk_log2,
        file_id=uuid.UUID(bytes=fid),
        plaintext_size=size,
        kid=kid,
        wrap_nonce=nonce,
        wrapped_dek=wrapped,
        stable=raw[:STABLE_LEN],
    )


def encrypt_stream(
    src: BinaryIO,
    dst: BinaryIO,
    *,
    size: int,
    file_id: uuid.UUID,
    keyring: Keyring,
    chunk_log2: int = DEFAULT_CHUNK_LOG2,
) -> EncryptResult:
    """Encrypt exactly ``size`` bytes from ``src`` into ``dst``."""
    stable = _stable_header(file_id, size, chunk_log2)
    dek = AESGCM.generate_key(bit_length=256)
    kid, wnonce, wrapped = keyring.wrap(dek, stable)
    dst.write(stable)
    dst.write(_wrap_section(kid, wnonce, wrapped))

    aes = AESGCM(dek)
    chunk = 1 << chunk_log2
    n = max(1, -(-size // chunk))
    digest = hashlib.sha256()
    written = HEADER_LEN
    remaining = size
    for i in range(n):
        want = min(chunk, remaining)
        buf = _read_exact(src, want)
        digest.update(buf)
        ct = aes.encrypt(_nonce(i, i == n - 1), buf, stable)
        dst.write(ct)
        written += len(ct)
        remaining -= want
    if src.read(1):
        raise EnvelopeError("source longer than declared size")
    return EncryptResult(ciphertext_size=written, plaintext_size=size, sha256=digest.digest())


def _read_exact(f: BinaryIO, n: int) -> bytes:
    parts: list[bytes] = []
    got = 0
    while got < n:
        b = f.read(n - got)
        if not b:
            raise EnvelopeError("source shorter than declared size")
        parts.append(b)
        got += len(b)
    return b"".join(parts)


def encrypt_file(src: Path, dst: Path, *, file_id: uuid.UUID, keyring: Keyring) -> EncryptResult:
    """Encrypt a file atomically (write to ``dst.part``, fsync, rename)."""
    size = src.stat().st_size
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    with src.open("rb") as fi, tmp.open("wb") as fo:
        res = encrypt_stream(fi, fo, size=size, file_id=file_id, keyring=keyring)
        fo.flush()
        os.fsync(fo.fileno())
    tmp.replace(dst)
    return res


def encrypt_bytes(data: bytes, *, file_id: uuid.UUID, keyring: Keyring) -> bytes:
    import io

    out = io.BytesIO()
    encrypt_stream(io.BytesIO(data), out, size=len(data), file_id=file_id, keyring=keyring)
    return out.getvalue()


def read_header(f: BinaryIO, *, expected_file_id: uuid.UUID | None = None) -> EnvelopeHeader:
    f.seek(0)
    hdr = _parse_header(f.read(HEADER_LEN))
    if expected_file_id is not None and hdr.file_id != expected_file_id:
        raise EnvelopeError("ciphertext belongs to a different file")
    f.seek(0, os.SEEK_END)
    actual = f.tell()
    if actual != hdr.ciphertext_size:
        raise EnvelopeError(f"ciphertext length mismatch ({actual} != {hdr.ciphertext_size})")
    return hdr


def _dek(hdr: EnvelopeHeader, keyring: Keyring) -> bytes:
    try:
        return keyring.unwrap(hdr.kid, hdr.wrap_nonce, hdr.wrapped_dek, hdr.stable)
    except InvalidTag as e:
        raise EnvelopeError("header authentication failed") from e


def decrypt_range(
    f: BinaryIO,
    keyring: Keyring,
    start: int = 0,
    end: int | None = None,
    *,
    expected_file_id: uuid.UUID | None = None,
) -> Iterator[bytes]:
    """Yield plaintext bytes ``[start, end]`` (inclusive), decrypting only needed chunks."""
    hdr = read_header(f, expected_file_id=expected_file_id)
    size = hdr.plaintext_size
    if end is None:
        end = size - 1
    if size == 0:
        # still authenticate the single empty chunk
        _decrypt_chunk(f, hdr, AESGCM(_dek(hdr, keyring)), 0)
        return
    if start < 0 or end >= size or start > end:
        raise EnvelopeError("range not satisfiable")
    aes = AESGCM(_dek(hdr, keyring))
    first = start // hdr.chunk_size
    last = end // hdr.chunk_size
    for i in range(first, last + 1):
        pt = _decrypt_chunk(f, hdr, aes, i)
        lo = start - i * hdr.chunk_size if i == first else 0
        hi = end - i * hdr.chunk_size + 1 if i == last else len(pt)
        yield pt[lo:hi]


def _decrypt_chunk(f: BinaryIO, hdr: EnvelopeHeader, aes: AESGCM, i: int) -> bytes:
    f.seek(hdr.chunk_offset(i))
    want = hdr.chunk_plain_len(i) + TAG_LEN
    ct = f.read(want)
    if len(ct) != want:
        raise EnvelopeError("truncated chunk")
    try:
        return aes.decrypt(_nonce(i, i == hdr.n_chunks - 1), ct, hdr.stable)
    except InvalidTag as e:
        raise EnvelopeError(f"chunk {i} failed authentication") from e


def decrypt_all(f: BinaryIO, keyring: Keyring, *, expected_file_id: uuid.UUID | None = None) -> bytes:
    return b"".join(decrypt_range(f, keyring, expected_file_id=expected_file_id))


def rewrap(path: Path, keyring: Keyring, new_kid: str | None = None) -> bool:
    """Re-wrap the DEK under another KEK, rewriting only the wrap section. Returns True if changed."""
    new_kid = new_kid or keyring.active_id
    with path.open("r+b") as f:
        hdr = read_header(f)
        if hdr.kid == new_kid:
            return False
        dek = _dek(hdr, keyring)
        kid, nonce, wrapped = keyring.wrap(dek, hdr.stable, kid=new_kid)
        f.seek(STABLE_LEN)
        f.write(_wrap_section(kid, nonce, wrapped))
        f.flush()
        os.fsync(f.fileno())
    return True
