"""Model weight provisioning on the worker.

Resolution order per file (first that works wins):
  1. already present locally with the expected size
  2. attached cache volume (read-only) -> symlinked, zero copy
  3. presigned object-storage URL (DO Spaces mirror) -> parallel ranged download
  4. Hugging Face (revision pinned) -> parallel ranged download

Weights are public model files; user content never goes through here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path

import httpx

from aivo_protocol.worker_api import FileFetch, FileSource, FileState, FilesStatus, PhaseTiming

log = logging.getLogger(__name__)

RANGE_SIZE = 64 * 1024 * 1024
PARALLEL_RANGES = 16
PARALLEL_FILES = 4


class FileManager:
    def __init__(self, models_dir: Path, *, simulate: bool = False, volume_mount: Path | None = None):
        self.models_dir = models_dir
        self.volume_mount = volume_mount
        self.simulate = simulate
        self.states: dict[str, FileState] = {}
        self.timings: list[PhaseTiming] = []
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._sem = asyncio.Semaphore(PARALLEL_FILES)
        self.models_dir.mkdir(parents=True, exist_ok=True)

    def present(self) -> list[str]:
        return sorted(d for d, s in self.states.items() if s.state == "ready")

    def status(self, dests: list[str] | None = None) -> FilesStatus:
        keys = dests if dests is not None else list(self.states)
        return FilesStatus(files=[self.states.get(d, FileState(dest=d)) for d in keys])

    def ensure(self, files: list[FileFetch]) -> FilesStatus:
        for f in files:
            st = self.states.get(f.dest)
            if st and st.state in ("ready", "downloading", "pending"):
                continue
            self.states[f.dest] = FileState(dest=f.dest, state="pending", bytes_total=f.bytes)
            self._tasks[f.dest] = asyncio.create_task(self._ensure_one(f), name=f"fetch:{f.dest}")
        return self.status([f.dest for f in files])

    async def _ensure_one(self, f: FileFetch) -> None:
        st = self.states[f.dest]
        t0 = time.monotonic()
        async with self._sem:
            try:
                if self.simulate:
                    await asyncio.sleep(0.05)
                    st.state, st.source, st.bytes_done = "ready", "simulated", f.bytes or 0
                    return
                dest = self._dest_path(f.dest)
                if dest.exists() and (f.bytes is None or dest.stat().st_size == f.bytes):
                    st.state, st.source, st.bytes_done = "ready", "local", dest.stat().st_size
                    return
                errors: list[str] = []
                for src in f.sources:
                    try:
                        st.state, st.source = "downloading", src.kind
                        await self._fetch(src, f, dest, st)
                        st.state = "ready"
                        st.seconds = time.monotonic() - t0
                        self.timings.append(
                            PhaseTiming(phase="file_fetch", seconds=st.seconds, source=src.kind, bytes=f.bytes)
                        )
                        if f.sha256 and src.kind != "volume":
                            asyncio.create_task(self._verify_later(dest, f.sha256, st))
                        return
                    except Exception as e:  # try the next source
                        log.warning("fetch %s via %s failed: %s", f.dest, src.kind, e)
                        errors.append(f"{src.kind}: {e}")
                st.state, st.error = "failed", "; ".join(errors) or "no sources"
            except Exception as e:
                st.state, st.error = "failed", str(e)

    def _dest_path(self, rel: str) -> Path:
        p = (self.models_dir / rel).resolve()
        if self.models_dir.resolve() not in p.parents:
            raise ValueError("destination escapes models dir")
        return p

    async def _fetch(self, src: FileSource, f: FileFetch, dest: Path, st: FileState) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.kind == "volume":
            if not self.volume_mount or not src.path:
                raise FileNotFoundError("no volume attached")
            vpath = (self.volume_mount / src.path).resolve()
            if self.volume_mount.resolve() not in vpath.parents or not vpath.exists():
                raise FileNotFoundError(str(src.path))
            if f.bytes is not None and vpath.stat().st_size != f.bytes:
                raise ValueError("size mismatch on volume copy")
            if dest.is_symlink() or dest.exists():
                dest.unlink()
            dest.symlink_to(vpath)
            st.bytes_done = vpath.stat().st_size
            return
        if not src.url:
            raise ValueError("source has no url")
        await download(src.url, dest, expected_size=f.bytes, headers=src.headers, state=st)

    async def _verify_later(self, path: Path, sha256: str, st: FileState) -> None:
        digest = await asyncio.to_thread(_sha256_file, path)
        if digest != sha256:
            log.error("sha256 mismatch for %s; quarantining", path)
            path.rename(path.with_suffix(path.suffix + ".bad"))
            st.state, st.error = "failed", "checksum mismatch"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


async def download(
    url: str,
    dest: Path,
    *,
    expected_size: int | None = None,
    headers: dict[str, str] | None = None,
    state: FileState | None = None,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Parallel ranged download to ``dest`` (via ``dest.part`` + rename)."""
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, read=300))
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        head = await client.get(url, headers={**(headers or {}), "Range": "bytes=0-0"})
        size = expected_size
        ranged = head.status_code == 206
        if ranged:
            cr = head.headers.get("content-range", "")
            if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                size = int(cr.rsplit("/", 1)[1])
        elif head.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {head.status_code}", request=head.request, response=head)
        final_url = str(head.url)
        if ranged and size and size > RANGE_SIZE:
            fd = os.open(part, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.ftruncate(fd, size)
                queue: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
                for start in range(0, size, RANGE_SIZE):
                    queue.put_nowait((start, min(size, start + RANGE_SIZE) - 1))

                async def worker() -> None:
                    while not queue.empty():
                        a, b = queue.get_nowait()
                        for attempt in range(4):
                            try:
                                async with client.stream(
                                    "GET", final_url, headers={**(headers or {}), "Range": f"bytes={a}-{b}"}
                                ) as r:
                                    if r.status_code != 206:
                                        raise RuntimeError(f"range request returned {r.status_code}")
                                    off = a
                                    async for chunk in r.aiter_bytes(1 << 20):
                                        os.pwrite(fd, chunk, off)
                                        off += len(chunk)
                                        if state:
                                            state.bytes_done += len(chunk)
                                    if off != b + 1:
                                        raise RuntimeError("short range")
                                break
                            except Exception:
                                if attempt == 3:
                                    raise
                                await asyncio.sleep(2**attempt)

                await asyncio.gather(*(worker() for _ in range(PARALLEL_RANGES)))
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            async with client.stream("GET", final_url, headers=headers or {}) as r:
                r.raise_for_status()
                with part.open("wb") as fh:
                    async for chunk in r.aiter_bytes(1 << 20):
                        fh.write(chunk)
                        if state:
                            state.bytes_done += len(chunk)
        if expected_size is not None and part.stat().st_size != expected_size:
            raise ValueError(f"size mismatch: got {part.stat().st_size}, expected {expected_size}")
        part.replace(dest)
    finally:
        if part.exists() and not dest.exists():
            part.unlink(missing_ok=True)
        if own:
            await client.aclose()
