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
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets

from aivo_protocol.worker_api import GpuInfo, JobIn, WorkerBootstrap
from aivo_worker.engines.base import EngineError, ProgressCb

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
        self._proc = await asyncio.create_subprocess_exec(*args, cwd=str(self.comfy_dir))
        for _ in range(600):
            if self._proc.returncode is not None:
                raise EngineError(f"ComfyUI exited with code {self._proc.returncode}")
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
        total_nodes = max(1, len(job.workflow))
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{self.port}/ws?clientId={client_id}", max_size=None, open_timeout=20
            ) as ws:
                r = await self._http.post("/prompt", json={"prompt": job.workflow, "client_id": client_id})
                if r.status_code != 200:
                    raise EngineError(f"workflow rejected: {_short(r.text)}", code="bad_request")
                prompt_id = r.json()["prompt_id"]
                progress(0.0, "loading_model")
                done_nodes: set[str] = set()
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, bytes):
                        continue  # previews (disabled anyway)
                    msg = json.loads(raw)
                    t, data = msg.get("type"), msg.get("data", {})
                    if data.get("prompt_id") not in (None, prompt_id):
                        continue
                    if t == "executing":
                        node = data.get("node")
                        if node is None:
                            break
                        done_nodes.add(node)
                    elif t == "execution_cached":
                        done_nodes.update(data.get("nodes") or [])
                    elif t == "progress":
                        frac = data.get("value", 0) / max(1, data.get("max", 1))
                        overall = 0.15 + 0.8 * frac
                        progress(min(0.95, overall), "generating")
                        continue
                    elif t == "execution_error":
                        text = f"{data.get('exception_type', '')}: {data.get('exception_message', '')}"
                        code = "oom" if any(h in text.lower() for h in OOM_HINTS) else "engine"
                        raise EngineError(_short(text), code=code)
                    elif t == "execution_interrupted":
                        raise EngineError("cancelled", code="cancelled")
                    elif t == "execution_success":
                        break
                    progress(min(0.15, 0.15 * len(done_nodes) / total_nodes), "loading_model")
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
