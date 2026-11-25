"""Model files on a RunPod global volume: downloaded by one worker, reused by the next (any data center)."""

from __future__ import annotations

import http.server
import os
import threading
from functools import partial

import pytest

from aivo_protocol.worker_api import FileFetch, FileSource
from aivo_worker.weights import SHARED_MARKER, FileManager

DEST = "diffusion_models/tiny.safetensors"


@pytest.fixture
def hf(tmp_path):
    """A stand-in for Hugging Face that counts downloads."""
    root = tmp_path / "hf"
    root.mkdir()
    (root / "tiny.safetensors").write_bytes(b"weights" * 50_000)
    hits: list[str] = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            super().do_GET()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/tiny.safetensors", hits
    srv.shutdown()


async def ready(fm: FileManager, url: str) -> str:
    fm.ensure([FileFetch(dest=DEST, sources=[FileSource(kind="hf", url=url)])])
    for t in list(fm._tasks.values()):
        await t
    st = fm.status([DEST]).files[0]
    assert st.state == "ready", st
    return st.source or ""


async def test_the_first_worker_downloads_and_keeps_it_the_next_reuses_it(tmp_path, hf):
    url, hits = hf
    shared = tmp_path / "runpod-volume" / "aivo" / "models"
    first = FileManager(tmp_path / "w1", shared=shared)
    assert await ready(first, url) == "hf"
    await first.settle(30)
    kept = shared / DEST
    assert kept.read_bytes() == (tmp_path / "w1" / DEST).read_bytes()
    assert (kept.parent / (kept.name + SHARED_MARKER)).exists()
    downloads = len(hits)

    second = FileManager(tmp_path / "w2", shared=shared)  # another worker, another data center
    assert await ready(second, url) == "volume"
    assert len(hits) == downloads  # nothing downloaded
    local = tmp_path / "w2" / DEST
    # copied onto the worker's own disk (reading in place through the mount was slow), in parallel ranges
    assert not local.is_symlink() and local.read_bytes() == kept.read_bytes()
    assert not local.with_name(local.name + ".part").exists()
    assert second.timings[-1].source == "volume" and second.timings[-1].bytes == 350_000


def test_parallel_ranges_copy_every_byte_in_order(tmp_path, monkeypatch):
    import aivo_worker.weights as w
    from aivo_protocol.worker_api import FileState

    monkeypatch.setattr(w, "SHARED_BLOCK", 4096)
    data = os.urandom(4096 * 37 + 123)  # not a whole number of ranges
    src, dst = tmp_path / "src.bin", tmp_path / "out" / "dst.bin"
    src.write_bytes(data)
    st = FileState(dest="x")
    w._copy_ranges(src, dst, len(data), 8, st)
    assert dst.read_bytes() == data and st.bytes_done == len(data)


async def test_a_failed_copy_from_the_volume_falls_back_to_a_download(tmp_path, hf, monkeypatch):
    import aivo_worker.weights as w

    url, hits = hf
    shared = tmp_path / "shared"
    first = FileManager(tmp_path / "w1", shared=shared)
    await ready(first, url)
    await first.settle(30)
    downloads = len(hits)

    def broken(*a, **k):
        raise OSError("transport endpoint is not connected")

    monkeypatch.setattr(w, "_copy_ranges", broken)
    second = FileManager(tmp_path / "w2", shared=shared)
    assert await ready(second, url) == "hf" and len(hits) > downloads


async def test_a_copy_without_its_marker_is_not_trusted(tmp_path, hf):
    url, hits = hf
    shared = tmp_path / "shared"
    (shared / DEST).parent.mkdir(parents=True)
    (shared / DEST).write_bytes(b"half")  # a worker stopped while copying
    fm = FileManager(tmp_path / "w", shared=shared)
    assert await ready(fm, url) == "hf" and hits
    await fm.settle(30)
    assert (shared / DEST).stat().st_size == 350_000  # replaced by the whole file, now marked complete


async def test_without_a_shared_volume_nothing_is_copied(tmp_path, hf):
    url, _ = hf
    fm = FileManager(tmp_path / "w")
    assert await ready(fm, url) == "hf"
    await fm.settle(1)
    assert not fm._publishing
