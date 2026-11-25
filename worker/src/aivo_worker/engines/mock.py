"""CPU-only mock engine: same contract as the ComfyUI engine, placeholder outputs.

Used by the fake provider (local dev, CI) so the full flow works without GPUs or
cloud spend. Outputs never contain the prompt.
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import math
import re
import shutil
import wave
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from aivo_protocol.worker_api import GpuInfo, JobIn
from aivo_worker.engines.base import EngineError, OutputsCb, ProgressCb
from aivo_worker.logbuf import LogBuffer


class MockEngine:
    name = "mock"

    def __init__(self, job_seconds: float = 2.0, *, fail_next: str | None = None, seen: dict[str, Any] | None = None):
        self.job_seconds = job_seconds
        self.seen = seen  # job id -> the workflow it ran (for tests of the simulated cloud)
        self.fail_next = fail_next  # chaos hook: "oom" | "engine"
        self.rewrite_reply: str | None = None  # chaos hook: what the language model says instead
        self.rewrite_fails = False  # chaos hook: the language model errors
        self.rewrites = 0
        self._ready = False
        self._loaded: list[str] = []
        self._cancel = asyncio.Event()
        self.logs: LogBuffer | None = None

    def _say(self, text: str, level: str | None = None) -> None:
        """Lines shaped like ComfyUI's own output, so the machine log looks realistic in dev."""
        if self.logs is not None:
            self.logs.emit("engine", text, level)

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def models_loaded(self) -> list[str]:
        return list(self._loaded)

    async def start(self) -> None:
        self._say("Total VRAM 49140 MB, total RAM 128000 MB (mock GPU)")
        self._say("Starting server")
        self._say("To see the GUI go to: http://127.0.0.1:8188")
        self._ready = True

    async def stop(self) -> None:
        self._ready = False

    def gpu(self) -> GpuInfo | None:
        return GpuInfo(name="Mock GPU", vram_total_gb=48.0, vram_free_gb=40.0)

    async def cancel(self) -> None:
        self._cancel.set()

    async def generate_text(self, workflow: dict[str, Any], output_node: str) -> str:
        """A stand-in for the language model: a longer description built from the short one."""
        self.rewrites += 1
        self._say("got prompt")
        self._say("Requested to load ZImageTEModel_")
        await asyncio.sleep(0.05)
        if self.rewrite_fails:
            self._say("!!! Exception during processing !!! simulated language model failure", "error")
            raise EngineError("simulated language model failure")
        if self.rewrite_reply is not None:
            return self.rewrite_reply
        text = next(
            (
                str(n["inputs"]["prompt"])
                for n in workflow.values()
                if isinstance(n, dict) and "prompt" in n.get("inputs", {})
            ),
            "",
        )
        m = re.search(r"Description: (.*)", text)
        desc = (m.group(1) if m else text).strip().rstrip(".")
        return (
            f"<think>\n\n</think>\n\n{desc[:1].upper()}{desc[1:]}, shown in rich detail: a clear subject, "
            "natural textures, a balanced composition with depth, soft directional light and a harmonious "
            "color palette, sharp focus."
        )

    async def run(
        self,
        job: JobIn,
        inputs_dir: Path,
        out_dir: Path,
        progress: ProgressCb,
        on_outputs: OutputsCb | None = None,
    ) -> list[Path]:
        self._cancel.clear()
        if self.seen is not None:
            self.seen[job.job_id] = job.workflow
            self.seen[f"{job.job_id}:more"] = job.more
        self._say("got prompt")
        t0 = asyncio.get_running_loop().time()
        if self.fail_next:
            code, self.fail_next = self.fail_next, None
            if code == "oom":
                self._say("torch.OutOfMemoryError: Allocation on device (simulated)", "error")
            else:
                self._say("!!! Exception during processing !!! simulated failure", "error")
            raise EngineError("simulated failure", code=code)
        if job.model_id not in self._loaded:
            progress(0.0, "loading_model")
            self._say(f"Requested to load model {job.model_id}")
            await asyncio.sleep(min(0.3, self.job_seconds / 4))
            self._loaded = [job.model_id]
            self._say(f"loaded completely; {job.model_id}")
        # one output at a time, like batches of one: each is handed over as soon as it's made
        out = job.output
        n_out = max(1, out.count)
        seed = int(hashlib.sha256(job.job_id.encode()).hexdigest()[:8], 16)
        steps = 10
        paths: list[Path] = []
        for k in range(n_out):
            for i in range(steps):
                if self._cancel.is_set():
                    self._say("Processing interrupted")
                    raise EngineError("cancelled", code="cancelled")
                progress((k + i / steps) / n_out, "generating")
                if i % 3 == 0:
                    bar = "█" * i + " " * (steps - i)
                    self._say(f"{i * 10:3d}%|{bar}| {i}/{steps} [00:0{i}<00:0{steps - i}]")
                await asyncio.sleep(self.job_seconds / steps / n_out)
            self._say(f"100%|{'█' * steps}| {steps}/{steps}")
            if out.kind == "image":
                path = await asyncio.to_thread(_image, out_dir / f"output_{k:02d}.png", out, seed + k)
            elif out.kind == "video":
                path = await _video(out_dir / f"output_{k:02d}.mp4", out, seed + k)
            else:
                path = await _audio(out_dir / f"output_{k:02d}", out, seed + k)
            paths.append(path)
            if on_outputs is not None:
                await on_outputs([path])
        self._say(f"Prompt executed in {asyncio.get_running_loop().time() - t0:.2f} seconds")
        progress(1.0, "saving")
        return paths


def _palette(seed: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    h = hashlib.sha256(str(seed).encode()).digest()
    return (h[0], h[1], h[2]), (h[3], h[4], h[5])


def _image(path: Path, out, seed: int) -> Path:  # type: ignore[no-untyped-def]
    w = min(out.width or 1024, 1536)
    h = min(out.height or 1024, 1536)
    a, b = _palette(seed)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(0, h):
        t = y / max(1, h - 1)
        row = tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))
        d.line([(0, y), (w, y)], fill=row)
    r = min(w, h) // 5
    d.ellipse((w // 2 - r, h // 2 - r, w // 2 + r, h // 2 + r), outline=(255, 255, 255), width=6)
    d.text((16, 16), "placeholder (mock GPU)", fill=(255, 255, 255))
    img.save(path, format="PNG")
    return path


async def _ffmpeg(*args: str) -> None:
    if not shutil.which("ffmpeg"):
        raise EngineError("ffmpeg is required for mock video/audio", code="engine")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise EngineError(f"ffmpeg failed: {err.decode()[-300:]}", code="engine")


async def _video(path: Path, out, seed: int) -> Path:  # type: ignore[no-untyped-def]
    w = min(out.width or 1280, 640)
    h = min(out.height or 720, 640)
    w, h = w - w % 2, h - h % 2
    dur = max(1.0, min(out.duration_s or 5.0, 10.0))
    fps = out.fps or 24
    args = ["-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}:duration={dur}"]
    if out.with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency={220 + seed % 400}:duration={dur}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if out.with_audio:
        args += ["-c:a", "aac", "-shortest"]
    args += ["-movflags", "+faststart", str(path)]
    await _ffmpeg(*args)
    return path


async def _audio(stem: Path, out, seed: int) -> Path:  # type: ignore[no-untyped-def]
    dur = max(1.0, min(out.duration_s or 30.0, 240.0))
    rate = 22050
    wav = stem.with_suffix(".wav")
    notes = [261.63, 293.66, 329.63, 392.0, 440.0, 523.25]
    samples = array.array("h")
    beat = rate // 4
    for i in range(int(dur * rate)):
        f = notes[((i // beat) + seed) % len(notes)]
        env = 0.5 * (1 - (i % beat) / beat)
        samples.append(int(12000 * env * math.sin(2 * math.pi * f * i / rate)))
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())
    if shutil.which("ffmpeg"):
        m4a = stem.with_suffix(".m4a")
        await _ffmpeg("-i", str(wav), "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(m4a))
        wav.unlink()
        return m4a
    return wav
