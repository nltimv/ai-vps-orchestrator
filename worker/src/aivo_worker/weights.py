"""Model weight provisioning on the worker.

Resolution order per file (first that works wins):
  1. already present locally with the expected size
  2. a RunPod global volume (shared): a whole, marked copy -> copied to local disk in parallel ranges
  3. attached cache volume (read-only) -> symlinked, zero copy
  4. presigned object-storage URL (DO Spaces mirror) -> parallel ranged download
  5. Hugging Face (revision pinned) -> parallel ranged download

Weights are public model files; user content never goes through here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path

import httpx

from aivo_protocol.worker_api import FileFetch, FileSource, FilesStatus, FileState, PhaseTiming
from aivo_worker.logbuf import LogBuffer

log = logging.getLogger(__name__)

_SOURCE_NAMES = {"volume": "the model disk", "url": "the model copy", "hf": "Hugging Face"}

RANGE_SIZE = 64 * 1024 * 1024
PARALLEL_RANGES = 16
PARALLEL_FILES = 4
SHARED_MARKER = ".aivo-complete"  # next to a file on the shared volume: {"bytes": n} once it's whole
# Object-backed volumes serve one reader slowly and many at once quickly. Measured on RunPod (Nov 2026): ComfyUI
# loading in place through the mount ~50 MB/s, one stream 140 MB/s, 32 parallel ranges ~400 MB/s (also when
# copying to local disk). So files are copied to the worker's disk in parallel ranges before use.
SHARED_READERS = int(os.environ.get("AIVO_SHARED_READERS", "32"))
SHARED_BLOCK = 32 * 1024 * 1024


class FileManager:
    def __init__(
        self,
        models_dir: Path,
        *,
        simulate: bool = False,
        volume_mount: Path | None = None,
        shared: Path | None = None,
        logs: LogBuffer | None = None,
    ):
        self.models_dir = models_dir
        self.logs = logs or LogBuffer()
        self.volume_mount = volume_mount
        # a RunPod global volume: object storage every worker can mount, anywhere. No rename, eventually
        # consistent: files are copied there whole after a download, then marked complete (SHARED_MARKER)
        self.shared = shared
        self._publishing: set[asyncio.Task[None]] = set()
        self.simulate = simulate
        self.states: dict[str, FileState] = {}
        self.timings: list[PhaseTiming] = []
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._sem = asyncio.Semaphore(PARALLEL_FILES)
        self._bg: set[asyncio.Task[None]] = set()
        self.models_dir.mkdir(parents=True, exist_ok=True)

    def present(self) -> list[str]:
        return sorted(d for d, s in self.states.items() if s.state == "ready")

    def status(self, dests: list[str] | None = None) -> FilesStatus:
        keys = dests if dests is not None else list(self.states)
        return FilesStatus(files=[self.states.get(d, FileState(dest=d)) for d in keys])

    def ensure(self, files: list[FileFetch]) -> FilesStatus:
        new: list[FileFetch] = []
        for f in files:
            st = self.states.get(f.dest)
            if st and st.state in ("ready", "downloading", "pending"):
                continue
            self.states[f.dest] = FileState(dest=f.dest, state="pending", bytes_total=f.bytes)
            self._tasks[f.dest] = asyncio.create_task(self._ensure_one(f), name=f"fetch:{f.dest}")
            new.append(f)
        if new:
            total = sum(f.bytes or 0 for f in new) / 1e9
            size = f", {total:.1f} GB" if all(f.bytes for f in new) else ""
            self.logs.emit("agent", f"model files requested: {len(new)} file(s){size}")
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
                    self.logs.emit("agent", f"{f.dest}: already on this machine")
                    return
                kept = self._shared_copy(f)
                if kept is not None and await self._from_shared(kept, dest, f, st):
                    return
                errors: list[str] = []
                for src in f.sources:
                    try:
                        st.state, st.source = "downloading", src.kind
                        self.logs.emit("agent", f"{f.dest}: fetching from {_SOURCE_NAMES.get(src.kind, src.kind)}")
                        await self._fetch(src, f, dest, st)
                        st.state = "ready"
                        st.seconds = time.monotonic() - t0
                        mb = st.bytes_done / 1e6
                        rate = f", {mb / st.seconds:.0f} MB/s" if st.seconds > 0.5 and src.kind != "volume" else ""
                        self.logs.emit("agent", f"{f.dest}: ready ({mb:,.0f} MB in {st.seconds:.0f} s{rate})")
                        self.timings.append(
                            PhaseTiming(phase="file_fetch", seconds=st.seconds, source=src.kind, bytes=f.bytes)
                        )
                        if f.sha256 and src.kind != "volume":
                            t = asyncio.create_task(self._verify_later(dest, f.sha256, st))
                            self._bg.add(t)
                            t.add_done_callback(self._bg.discard)
                        if self.shared is not None and src.kind != "volume":
                            t = asyncio.create_task(self._publish(dest, f.dest), name=f"publish:{f.dest}")
                            self._publishing.add(t)
                            t.add_done_callback(self._publishing.discard)
                        return
                    except Exception as e:  # try the next source
                        self.logs.emit("agent", f"{f.dest}: {src.kind} failed: {e}", "warning")
                        errors.append(f"{src.kind}: {e}")
                st.state, st.error = "failed", "; ".join(errors) or "no sources"
                self.logs.emit("agent", f"{f.dest}: could not be fetched: {st.error}", "error")
            except Exception as e:
                st.state, st.error = "failed", str(e)
                self.logs.emit("agent", f"{f.dest}: could not be fetched: {e}", "error")

    # -- the shared (global) volume ------------------------------------------------------

    def _shared_path(self, rel: str) -> Path | None:
        if self.shared is None:
            return None
        p = (self.shared / rel).resolve()
        return p if self.shared.resolve() in p.parents else None

    def _shared_copy(self, f: FileFetch) -> Path | None:
        """The file on the shared volume, if a worker finished copying it there (its marker says how big)."""
        p = self._shared_path(f.dest)
        if p is None:
            return None
        try:
            marked = json.loads(p.with_name(p.name + SHARED_MARKER).read_text()).get("bytes")
            size = p.stat().st_size
        except (OSError, ValueError):
            return None
        if size != marked or (f.bytes is not None and size != f.bytes):
            return None
        return p

    async def _from_shared(self, kept: Path, dest: Path, f: FileFetch, st: FileState) -> bool:
        """Copy a file from the shared volume to this worker's disk, in parallel ranges. False: download it instead."""
        t0 = time.monotonic()
        size = kept.stat().st_size
        st.state, st.source, st.bytes_done = "downloading", "volume", 0
        self.logs.emit("agent", f"{f.dest}: copying from the shared volume (no download)")
        try:
            await asyncio.to_thread(_copy_ranges, kept, dest, size, SHARED_READERS, st)
        except Exception as e:
            self.logs.emit(
                "agent", f"{f.dest}: copy from the shared volume failed ({e}); downloading instead", "warning"
            )
            dest.with_name(dest.name + ".part").unlink(missing_ok=True)
            return False
        secs = time.monotonic() - t0
        st.state, st.bytes_done, st.seconds = "ready", size, secs
        rate = f", {size / 1e6 / secs:,.0f} MB/s" if secs > 0.5 else ""
        self.logs.emit("agent", f"{f.dest}: ready from the shared volume ({size / 1e6:,.0f} MB in {secs:.0f} s{rate})")
        self.timings.append(PhaseTiming(phase="file_fetch", seconds=secs, source="volume", bytes=size))
        return True

    async def _publish(self, local: Path, rel: str) -> None:
        """Copy a downloaded file onto the shared volume for later workers: whole, then marked complete."""
        target = self._shared_path(rel)
        if target is None:
            return
        try:
            size = local.stat().st_size
            marker = target.with_name(target.name + SHARED_MARKER)
            if self._shared_copy(FileFetch(dest=rel, bytes=size)) is not None:
                return  # another worker kept it already
            t0 = time.monotonic()
            await asyncio.to_thread(_copy_whole, local, target)
            if target.stat().st_size != size:
                raise OSError(f"copied {target.stat().st_size} of {size} bytes")
            marker.write_text(json.dumps({"bytes": size}))
            secs = time.monotonic() - t0
            self.logs.emit(
                "agent", f"{rel}: kept on the shared volume for later workers ({size / 1e6:,.0f} MB in {secs:.0f} s)"
            )
        except Exception as e:  # later workers just download it again
            self.logs.emit("agent", f"{rel}: could not keep it on the shared volume: {e}", "warning")

    async def settle(self, timeout: float) -> None:
        """Wait (up to ``timeout``) for copies to the shared volume, so they aren't cut off when the worker stops."""
        pending = {t for t in self._publishing if not t.done()}
        if not pending:
            return
        self.logs.emit("agent", f"keeping {len(pending)} model file(s) on the shared volume for later workers")
        await asyncio.wait(pending, timeout=timeout)

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
            self.logs.emit("agent", f"{path.name}: checksum mismatch, file discarded", "error")
            path.rename(path.with_suffix(path.suffix + ".bad"))
            st.state, st.error = "failed", "checksum mismatch"


def _copy_ranges(src: Path, dst: Path, size: int, readers: int, st: FileState) -> None:
    """Copy ``src`` to ``dst`` with ``readers`` parallel range reads; ``dst`` appears only once it's whole."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    part = dst.with_name(dst.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    lock = Lock()
    fi = os.open(src, os.O_RDONLY)
    fo = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)

    def one(offset: int) -> None:
        end = min(offset + SHARED_BLOCK, size)
        while offset < end:
            block = os.pread(fi, end - offset, offset)
            if not block:
                raise OSError(f"the volume returned less than {size} bytes")
            view = memoryview(block)
            while view:
                n = os.pwrite(fo, view, offset)
                view, offset = view[n:], offset + n
            with lock:
                st.bytes_done = (st.bytes_done or 0) + len(block)

    try:
        os.ftruncate(fo, size)
        with ThreadPoolExecutor(max(1, readers)) as ex:
            list(ex.map(one, range(0, size, SHARED_BLOCK)))
        os.fsync(fo)
    finally:
        os.close(fi)
        os.close(fo)
    part.replace(dst)


def _copy_whole(src: Path, dst: Path) -> None:
    """Sequential copy (object-backed storage: no rename, so the marker written afterwards says it's whole)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as fi, dst.open("wb") as fo:
        while block := fi.read(16 * 1024 * 1024):
            fo.write(block)


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
    # unique per download: on a shared RunPod network volume several workers may fetch the same file
    part = dest.with_name(f"{dest.name}.{os.getpid()}-{secrets.token_hex(4)}.part")
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
