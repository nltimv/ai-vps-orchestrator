"""Output post-processing on the worker.

* strips embedded metadata (ComfyUI writes the full workflow, including the
  prompt, into PNG text chunks and video tags)
* makes MP4/M4A seekable over HTTP Range (``+faststart``)
* computes sha256 so the backend can verify what it downloaded
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import shutil
from pathlib import Path

from PIL import Image

from aivo_protocol.worker_api import OutputMeta

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
}


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, (out or err).decode(errors="replace")


async def postprocess(path: Path) -> OutputMeta:
    ext = path.suffix.lower()
    width = height = None
    duration = None
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        width, height = await asyncio.to_thread(_strip_image, path)
    elif shutil.which("ffmpeg") and ext in (".mp4", ".webm", ".m4a", ".mp3", ".flac", ".wav"):
        target = path
        if ext in (".flac", ".wav"):
            target = path.with_suffix(".m4a")  # universally playable, small
            code, _ = await _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                                 "-map_metadata", "-1", "-c:a", "aac", "-b:a", "256k",
                                 "-movflags", "+faststart", str(target))
            if code == 0:
                path.unlink()
                path = target
        else:
            tmp = path.with_name(path.stem + ".clean" + path.suffix)
            extra = ["-movflags", "+faststart"] if ext in (".mp4", ".m4a") else []
            code, _ = await _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                                 "-map_metadata", "-1", "-c", "copy", *extra, str(tmp))
            if code == 0:
                tmp.replace(path)
        width, height, duration = await probe(path)
    sha = await asyncio.to_thread(_sha256, path)
    media_type = MEDIA_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return OutputMeta(
        name=path.name,
        media_type=media_type,
        bytes=path.stat().st_size,
        sha256=sha,
        width=width,
        height=height,
        duration_s=duration,
    )


def _strip_image(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        im.load()
        size = im.size
        clean = im.copy()
    clean.info = {}  # drop text chunks / EXIF (ComfyUI embeds the workflow incl. prompt)
    fmt = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}[path.suffix.lower()]
    clean.save(path, format=fmt, **({"quality": 95} if fmt in ("JPEG", "WEBP") else {}))
    return size


async def probe(path: Path) -> tuple[int | None, int | None, float | None]:
    if not shutil.which("ffprobe"):
        return None, None, None
    code, out = await _run("ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                           "-show_format", str(path))
    if code != 0:
        return None, None, None
    data = json.loads(out)
    w = h = None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            w, h = s.get("width"), s.get("height")
    dur = data.get("format", {}).get("duration")
    return w, h, float(dur) if dur else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()
