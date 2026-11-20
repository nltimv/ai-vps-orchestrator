"""Encrypted media store on the backend's own volume.

This is deliberately the *only* media store implementation: generated content is
never written to the cloud provider's storage (snapshots, Spaces, volumes there
hold public model weights only).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from aivo.storage import envelope
from aivo.storage.keyring import Keyring


@dataclass(frozen=True)
class StoredFile:
    asset_id: uuid.UUID
    storage_path: str
    plaintext_size: int
    ciphertext_size: int
    key_id: str
    content_mac: str


class FilesystemStore:
    def __init__(self, root: Path, keyring: Keyring):
        self.root = root
        self.keyring = keyring
        self.root.mkdir(parents=True, exist_ok=True)

    def _rel(self, asset_id: uuid.UUID) -> str:
        h = asset_id.hex
        return f"{h[:2]}/{h[2:4]}/{h}.enc"

    def path(self, storage_path: str) -> Path:
        p = (self.root / storage_path).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("storage path escapes media root")
        return p

    def put_file(self, asset_id: uuid.UUID, src: Path) -> StoredFile:
        rel = self._rel(asset_id)
        res = envelope.encrypt_file(src, self.path(rel), file_id=asset_id, keyring=self.keyring)
        return StoredFile(
            asset_id=asset_id,
            storage_path=rel,
            plaintext_size=res.plaintext_size,
            ciphertext_size=res.ciphertext_size,
            key_id=self.keyring.active_id,
            content_mac=self.keyring.content_mac(res.sha256),
        )

    def put_bytes(self, asset_id: uuid.UUID, data: bytes) -> StoredFile:
        import hashlib

        rel = self._rel(asset_id)
        dst = self.path(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        blob = envelope.encrypt_bytes(data, file_id=asset_id, keyring=self.keyring)
        tmp = dst.with_suffix(".enc.part")
        with tmp.open("wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(dst)
        return StoredFile(
            asset_id=asset_id,
            storage_path=rel,
            plaintext_size=len(data),
            ciphertext_size=len(blob),
            key_id=self.keyring.active_id,
            content_mac=self.keyring.content_mac(hashlib.sha256(data).digest()),
        )

    def read_range(
        self, asset_id: uuid.UUID, storage_path: str, start: int = 0, end: int | None = None
    ) -> Iterator[bytes]:
        with self.path(storage_path).open("rb") as f:
            yield from envelope.decrypt_range(f, self.keyring, start, end, expected_file_id=asset_id)

    def read_all(self, asset_id: uuid.UUID, storage_path: str) -> bytes:
        return b"".join(self.read_range(asset_id, storage_path))

    def delete(self, storage_path: str) -> None:
        try:
            self.path(storage_path).unlink()
        except FileNotFoundError:
            pass

    def rewrap_all(self, new_kid: str | None = None) -> int:
        changed = 0
        for p in self.root.rglob("*.enc"):
            if envelope.rewrap(p, self.keyring, new_kid):
                changed += 1
        return changed
