import io
import os
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aivo.storage import envelope
from aivo.storage.envelope import EnvelopeError
from aivo.storage.keyring import Keyring
from aivo.storage.media_store import FilesystemStore

KR = Keyring.parse(Keyring.generate_spec("k1") + "," + Keyring.generate_spec("k2"), "k1")
CHUNK = 1 << envelope.DEFAULT_CHUNK_LOG2


def enc(data: bytes, fid: uuid.UUID | None = None) -> tuple[uuid.UUID, bytes]:
    fid = fid or uuid.uuid4()
    return fid, envelope.encrypt_bytes(data, file_id=fid, keyring=KR)


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK + 17])
def test_roundtrip_sizes(size):
    data = os.urandom(size)
    fid, blob = enc(data)
    assert envelope.decrypt_all(io.BytesIO(blob), KR, expected_file_id=fid) == data
    hdr = envelope.read_header(io.BytesIO(blob))
    assert hdr.ciphertext_size == len(blob)


def test_large_roundtrip():
    data = os.urandom(10 * 1024 * 1024 + 5)
    fid, blob = enc(data)
    assert envelope.decrypt_all(io.BytesIO(blob), KR, expected_file_id=fid) == data


@settings(max_examples=60, deadline=None)
@given(st.integers(min_value=1, max_value=4 * CHUNK + 100), st.data())
def test_any_range_matches_slice(size, data):
    pt = bytes((i * 7) % 251 for i in range(size))
    fid, blob = enc(pt)
    a = data.draw(st.integers(min_value=0, max_value=size - 1))
    b = data.draw(st.integers(min_value=a, max_value=size - 1))
    got = b"".join(envelope.decrypt_range(io.BytesIO(blob), KR, a, b, expected_file_id=fid))
    assert got == pt[a : b + 1]


def _flip(blob: bytes, pos: int) -> bytes:
    b = bytearray(blob)
    b[pos] ^= 0x01
    return bytes(b)


def test_tamper_detection():
    data = os.urandom(2 * CHUNK + 10)
    fid, blob = enc(data)
    for pos in [3, 20, 40, envelope.STABLE_LEN + 70, envelope.HEADER_LEN + 5, len(blob) - 1]:
        with pytest.raises(EnvelopeError):
            envelope.decrypt_all(io.BytesIO(_flip(blob, pos)), KR, expected_file_id=fid)


def test_truncation_append_reorder():
    data = os.urandom(3 * CHUNK)
    fid, blob = enc(data)
    with pytest.raises(EnvelopeError):
        envelope.decrypt_all(io.BytesIO(blob[:-1]), KR)
    with pytest.raises(EnvelopeError):
        envelope.decrypt_all(io.BytesIO(blob + b"x" * 16), KR)
    # drop the final chunk: length check catches it
    with pytest.raises(EnvelopeError):
        envelope.decrypt_all(io.BytesIO(blob[: envelope.HEADER_LEN + 2 * (CHUNK + 16)]), KR)
    # swap chunks 0 and 1
    h = envelope.HEADER_LEN
    c = CHUNK + 16
    swapped = blob[:h] + blob[h + c : h + 2 * c] + blob[h : h + c] + blob[h + 2 * c :]
    with pytest.raises(EnvelopeError):
        envelope.decrypt_all(io.BytesIO(swapped), KR)


def test_header_from_other_file_rejected():
    fid_a, blob_a = enc(b"a" * 100)
    fid_b, blob_b = enc(b"b" * 100)
    franken = blob_a[: envelope.HEADER_LEN] + blob_b[envelope.HEADER_LEN :]
    with pytest.raises(EnvelopeError):
        envelope.decrypt_all(io.BytesIO(franken), KR)
    with pytest.raises(EnvelopeError):
        envelope.decrypt_all(io.BytesIO(blob_a), KR, expected_file_id=fid_b)


def test_rewrap_preserves_content(tmp_path):
    data = os.urandom(CHUNK + 3)
    fid = uuid.uuid4()
    p = tmp_path / "x.enc"
    p.write_bytes(envelope.encrypt_bytes(data, file_id=fid, keyring=KR))
    assert envelope.read_header(p.open("rb")).kid == "k1"
    assert envelope.rewrap(p, KR, "k2") is True
    assert envelope.read_header(p.open("rb")).kid == "k2"
    assert envelope.decrypt_all(p.open("rb"), KR, expected_file_id=fid) == data
    only_k2 = Keyring(keys={"k2": KR.keys["k2"]}, active_id="k2")
    assert envelope.decrypt_all(p.open("rb"), only_k2, expected_file_id=fid) == data


def test_wrong_key_fails():
    fid, blob = enc(b"hello")
    other = Keyring.parse(Keyring.generate_spec("k1"))
    with pytest.raises(EnvelopeError):
        envelope.decrypt_all(io.BytesIO(blob), other)


def test_field_seal_roundtrip():
    sealed = KR.seal("a cat on the moon", b"job:1")
    assert b"cat" not in sealed
    assert KR.open(sealed, b"job:1") == "a cat on the moon"
    with pytest.raises(Exception):
        KR.open(sealed, b"job:2")


def test_store_has_no_plaintext_magic(tmp_path):
    store = FilesystemStore(tmp_path, KR)
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(1000)
    aid = uuid.uuid4()
    sf = store.put_bytes(aid, png)
    raw = store.path(sf.storage_path).read_bytes()
    assert b"PNG" not in raw
    assert store.read_all(aid, sf.storage_path) == png
    assert b"".join(store.read_range(aid, sf.storage_path, 1, 3)) == b"PNG"


def test_keyring_parse_errors():
    with pytest.raises(Exception):
        Keyring.parse("")
    with pytest.raises(Exception):
        Keyring.parse("k1:notbase64!!")
    with pytest.raises(Exception):
        Keyring.parse("k1:" + "QUJD")  # 3 bytes
