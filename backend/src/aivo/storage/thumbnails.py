"""Thumbnails / posters / waveforms, generated in RAM-backed scratch and stored encrypted."""

from __future__ import annotations

import asyncio
import io
import shutil
from pathlib import Path

from PIL import Image

THUMB_PX = 512


def image_thumb(src: Path) -> bytes:
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((THUMB_PX, THUMB_PX))
        buf = io.BytesIO()
        im.save(buf, format="WEBP", quality=82)
        return buf.getvalue()


async def _ffmpeg_png(*args: str) -> bytes | None:
    if not shutil.which("ffmpeg"):
        return None
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        *args,
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out if proc.returncode == 0 and out else None


async def video_poster(src: Path, at_s: float = 1.0) -> bytes | None:
    png = await _ffmpeg_png("-ss", str(at_s), "-i", str(src), "-frames:v", "1", "-vf", f"scale={THUMB_PX}:-2")
    if png is None:
        png = await _ffmpeg_png("-i", str(src), "-frames:v", "1", "-vf", f"scale={THUMB_PX}:-2")
    return _png_to_webp(png) if png else None


async def audio_waveform(src: Path) -> bytes | None:
    png = await _ffmpeg_png(
        "-i",
        str(src),
        "-filter_complex",
        "showwavespic=s=1024x256:colors=#8b5cf6|#ec4899:split_channels=0",
        "-frames:v",
        "1",
    )
    return _png_to_webp(png) if png else None


def _png_to_webp(png: bytes) -> bytes:
    with Image.open(io.BytesIO(png)) as im:
        buf = io.BytesIO()
        im.convert("RGBA").save(buf, format="WEBP", quality=82)
        return buf.getvalue()


async def make_thumbnail(src: Path, media_type: str) -> bytes | None:
    if media_type.startswith("image/"):
        return await asyncio.to_thread(image_thumb, src)
    if media_type.startswith("video/"):
        return await video_poster(src)
    if media_type.startswith("audio/"):
        return await audio_waveform(src)
    return None
