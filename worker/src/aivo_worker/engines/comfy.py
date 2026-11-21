"""ComfyUI engine: runs headless ComfyUI on 127.0.0.1 and executes API-format workflows.

* all of ComfyUI's input/output/temp directories live on the tmpfs job dir
* ``--disable-metadata`` keeps prompts out of saved files (we strip again anyway)
* when switching to a different model, VRAM is freed first
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import httpx
import websockets

from aivo_protocol.worker_api import GpuInfo, JobIn, WorkerBootstrap
from aivo_worker.engines.base import EngineError, ProgressCb
from aivo_worker.logbuf import LogBuffer

log = logging.getLogger(__name__)

MODEL_FOLDERS = (
    "checkpoints",
    "diffusion_models",
    "unet",
    "text_encoders",
    "clip",
    "vae",
    "loras",
    "clip_vision",
    "upscale_models",
    "audio_encoders",
    "model_patches",
    "latent_upscale_models",
)
OOM_HINTS = ("out of memory", "outofmemory", "cuda error: out of memory", "hip out of memory", "allocation")
PROGRESS_EVERY_S = 3.0  # ComfyUI redraws progress bars with \r many times a second; keep one every few s
_PERCENT = re.compile(r"(\d{1,3})%\|")


class ComfyEngine:
    name = "comfy"

    def __init__(
        self,
        comfy_dir: Path,
        models_dir: Path,
        work_dir: Path,
        *,
        port: int = 8188,
        extra_args: list[str] | None = None,
    ):
        self.comfy_dir = comfy_dir
        self.models_dir = models_dir
        self.work_dir = work_dir  # tmpfs
        self.port = port
        self.extra_args = extra_args or []
        self.base = f"http://127.0.0.1:{port}"
        self._proc: asyncio.subprocess.Process | None = None
        self._ready = False
        self._loaded: list[str] = []
        self._gpu: GpuInfo | None = None
        self._http = httpx.AsyncClient(base_url=self.base, timeout=60, trust_env=False)
        self._watch: asyncio.Task[None] | None = None
        self._pump: asyncio.Task[None] | None = None
        self.logs: LogBuffer | None = None
        self.recent: deque[str] = deque(maxlen=30)  # last output lines, for error messages
        self._last_progress = 0.0

    @classmethod
    def from_env(cls, boot: WorkerBootstrap) -> ComfyEngine:
        return cls(
            comfy_dir=Path(os.environ.get("AIVO_COMFY_DIR", "/opt/ComfyUI")),
            models_dir=Path(boot.models_dir),
            work_dir=Path(boot.job_dir) / "comfy",
            extra_args=os.environ.get("AIVO_COMFY_ARGS", "").split(),
        )

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def models_loaded(self) -> list[str]:
        return list(self._loaded)

    def gpu(self) -> GpuInfo | None:
        return self._gpu

    def _write_model_paths(self) -> Path:
        cfg = self.work_dir / "extra_model_paths.yaml"
        lines = ["aivo:", f"  base_path: {self.models_dir}"]
        for f in MODEL_FOLDERS:
            (self.models_dir / f).mkdir(parents=True, exist_ok=True)
            lines.append(f"  {f}: {f}/")
        cfg.write_text("\n".join(lines) + "\n")
        return cfg

    async def start(self) -> None:
        for d in ("input", "output", "temp", "user"):
            (self.work_dir / d).mkdir(parents=True, exist_ok=True)
        cfg = self._write_model_paths()
        args = [
            sys.executable,
            str(self.comfy_dir / "main.py"),
            "--listen",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--input-directory",
            str(self.work_dir / "input"),
            "--output-directory",
            str(self.work_dir / "output"),
            "--temp-directory",
            str(self.work_dir / "temp"),
            "--user-directory",
            str(self.work_dir / "user"),
            "--extra-model-paths-config",
            str(cfg),
            "--disable-metadata",
            "--preview-method",
            "none",
            "--disable-auto-launch",
            *self.extra_args,
        ]
        log.info("starting ComfyUI: %s", " ".join(args))
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(self.comfy_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        assert self._proc.stdout is not None
        self._pump = asyncio.create_task(self._pump_output(self._proc.stdout), name="comfy-output")
        for _ in range(600):
            if self._proc.returncode is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(self._pump), 2)  # collect the last words
                tail = " | ".join(list(self.recent)[-3:])
                raise EngineError(
                    f"ComfyUI exited with code {self._proc.returncode}" + (f": {_short(tail, 300)}" if tail else "")
                )
            try:
                r = await self._http.get("/system_stats", timeout=2)
                if r.status_code == 200:
                    self._gpu = _gpu_from_stats(r.json())
                    self._ready = True
                    self._watch = asyncio.create_task(self._watch_proc())
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        raise EngineError("ComfyUI did not start in time")

    async def _pump_output(self, stream: asyncio.StreamReader) -> None:
        """Copy ComfyUI's output to our stdout (docker logs) and into the machine log."""
        out = getattr(sys.stdout, "buffer", None)
        pending = ""
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            if out is not None:
                with contextlib.suppress(Exception):
                    out.write(chunk)
                    out.flush()
            pending += chunk.decode("utf-8", errors="replace")
            # A line ends at \n; a progress bar redraw ends at \r.
            parts = re.split(r"(\r\n|\n|\r)", pending)
            pending = parts.pop()  # incomplete remainder
            for text, sep in zip(parts[::2], parts[1::2], strict=True):
                self._line(text, redraw=sep == "\r")
        if pending:
            self._line(pending, redraw=False)

    def _line(self, text: str, *, redraw: bool) -> None:
        text = text.rstrip()
        if not text.strip():
            return
        if redraw or _PERCENT.search(text):
            m = _PERCENT.search(text)
            done = m is not None and m.group(1) == "100"
            now = time.monotonic()
            if not done and now - self._last_progress < PROGRESS_EVERY_S:
                return
            self._last_progress = now
        self.recent.append(text)
        if self.logs is not None:
            low = text.lower()
            level = "error" if ("error" in low or "traceback" in low or "exception" in low) else None
            self.logs.emit("engine", text, level)

    async def _watch_proc(self) -> None:
        assert self._proc is not None
        await self._proc.wait()
        self._ready = False
        log.error("ComfyUI exited (code %s)", self._proc.returncode)

    async def stop(self) -> None:
        self._ready = False
        if self._watch:
            self._watch.cancel()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._proc.wait(), 15)
            if self._proc.returncode is None:
                self._proc.kill()
        if self._pump:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._pump, 5)
            self._pump.cancel()
        await self._http.aclose()

    async def cancel(self) -> None:
        with contextlib.suppress(httpx.HTTPError):
            await self._http.post("/interrupt")

    async def _free_if_switching(self, model_id: str) -> None:
        if self._loaded and self._loaded != [model_id]:
            with contextlib.suppress(httpx.HTTPError):
                await self._http.post("/free", json={"unload_models": True, "free_memory": True})
        self._loaded = [model_id]

    async def run(self, job: JobIn, inputs_dir: Path, out_dir: Path, progress: ProgressCb) -> list[Path]:
        if not self._ready:
            raise EngineError("engine not ready")
        await self._free_if_switching(job.model_id)
        comfy_in = self.work_dir / "input"
        staged: list[Path] = []
        for name in job.inputs:
            src = inputs_dir / name
            if src.exists():
                dst = comfy_in / name
                shutil.copyfile(src, dst)
                staged.append(dst)
        client_id = uuid.uuid4().hex
        tracker = _ProgressTracker(job.workflow, progress)
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{self.port}/ws?clientId={client_id}", max_size=None, open_timeout=20
            ) as ws:
                r = await self._http.post("/prompt", json={"prompt": job.workflow, "client_id": client_id})
                if r.status_code != 200:
                    raise EngineError(f"workflow rejected: {_short(r.text)}", code="bad_request")
                prompt_id = r.json()["prompt_id"]
                tracker.report(0.0, "loading_model")
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, bytes):
                        continue  # previews (disabled anyway)
                    msg = json.loads(raw)
                    t, data = msg.get("type"), msg.get("data", {})
                    if data.get("prompt_id") not in (None, prompt_id):
                        continue
                    if t == "executing":
                        if data.get("node") is None:
                            break
                        tracker.node_started(str(data["node"]))
                    elif t == "execution_cached":
                        tracker.nodes_cached([str(n) for n in data.get("nodes") or []])
                    elif t == "progress":
                        tracker.sampling(str(data.get("node") or "?"), data.get("value", 0), data.get("max", 1))
                    elif t == "execution_error":
                        text = f"{data.get('exception_type', '')}: {data.get('exception_message', '')}"
                        code = "oom" if any(h in text.lower() for h in OOM_HINTS) else "engine"
                        raise EngineError(_short(text), code=code)
                    elif t == "execution_interrupted":
                        raise EngineError("cancelled", code="cancelled")
                    elif t == "execution_success":
                        break
                    # anything else (status, execution_start, progress_state, executed, ...) changes nothing
            hist = (await self._http.get(f"/history/{prompt_id}")).json().get(prompt_id, {})
            files = _collect_outputs(hist.get("outputs", {}))
            if not files:
                raise EngineError("the workflow produced no output files")
            out: list[Path] = []
            base = (self.work_dir / "output").resolve()
            for i, (sub, fname) in enumerate(files):
                src = (base / sub / fname).resolve()
                if base not in src.parents or not src.exists():
                    continue
                dst = out_dir / f"output_{i:02d}{src.suffix.lower()}"
                shutil.move(str(src), dst)
                out.append(dst)
            with contextlib.suppress(httpx.HTTPError):
                await self._http.post("/history", json={"delete": [prompt_id]})
            return out
        finally:
            for p in staged:
                p.unlink(missing_ok=True)


SAMPLER_TYPES = {"KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced"}


class _ProgressTracker:
    """Turns ComfyUI's websocket messages into one progress value that only moves forward.

    0-15 %: loading the model (nodes before sampling); 15-95 %: the sampling steps of all samplers in
    the workflow (e.g. Wan 2.2's high- and low-noise passes count as one run); then decoding/saving.
    Once creating has started, the stage never goes back to loading the model, even if the workflow
    loads a second model between samplers.
    """

    def __init__(self, workflow: dict[str, Any], progress: ProgressCb):
        self._cb = progress
        self._total_nodes = max(1, len(workflow))
        self._samplers = {
            str(nid)
            for nid, node in workflow.items()
            if isinstance(node, dict) and str(node.get("class_type", "")) in SAMPLER_TYPES
        }
        self._done: set[str] = set()
        self._steps: dict[str, float] = {}
        self._stage = "loading_model"
        self._value = 0.0

    def report(self, value: float, stage: str) -> None:
        if stage == "loading_model" and self._stage != "loading_model":
            return
        value = max(self._value, min(0.95, value))
        if value == self._value and stage == self._stage and value > 0:
            return
        self._value, self._stage = value, stage
        self._cb(value, stage)

    def node_started(self, node: str) -> None:
        self._done.add(node)
        self.report(0.15 * len(self._done) / self._total_nodes, "loading_model")

    def nodes_cached(self, nodes: list[str]) -> None:
        self._done.update(nodes)
        self.report(0.15 * len(self._done) / self._total_nodes, "loading_model")

    def sampling(self, node: str, value: float, maximum: float) -> None:
        self._steps[node] = max(0.0, min(1.0, value / max(1, maximum)))
        samplers = max(len(self._samplers | set(self._steps)), 1)
        self.report(0.15 + 0.8 * sum(self._steps.values()) / samplers, "generating")


def _collect_outputs(outputs: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for node_out in outputs.values():
        for value in node_out.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and item.get("filename") and item.get("type", "output") == "output":
                    found.append((item.get("subfolder", ""), item["filename"]))
    return found


def _gpu_from_stats(stats: dict[str, Any]) -> GpuInfo | None:
    devs = stats.get("devices") or []
    if not devs:
        return None
    d = devs[0]
    return GpuInfo(
        name=str(d.get("name", "GPU")),
        vram_total_gb=float(d.get("vram_total", 0)) / 1e9,
        vram_free_gb=float(d.get("vram_free", 0)) / 1e9,
    )


def _short(s: str, n: int = 400) -> str:
    return s if len(s) <= n else s[:n] + "…"
