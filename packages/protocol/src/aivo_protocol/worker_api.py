"""Wire contract between the backend (client) and the worker agent (server).

The backend *pushes* work to the worker over HTTPS. The worker never calls the
backend, so it never needs to know where the backend lives.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from aivo_protocol.version import PROTOCOL_VERSION

OutputKind = Literal["image", "video", "audio"]
EngineName = Literal["comfy", "mock"]


class WatchdogConfig(BaseModel):
    """Opt-in safety net: the worker deletes its own machine if the backend goes silent.

    Uses a provider token scoped to deleting machines. Recommended only inside a
    dedicated cloud account/team, because such a token can delete any machine there.
    """

    enabled: bool = False
    provider: str = "digitalocean"
    token: str | None = None
    max_silence_s: int = 1200
    deadline_unix: float | None = None


class WorkerBootstrap(BaseModel):
    """Everything a freshly booted worker needs. Delivered via cloud-init user_data.

    Never contains user content (prompts, images): those only travel over the
    pinned-TLS channel, and only into RAM-backed directories.
    """

    protocol: str = PROTOCOL_VERSION
    instance_id: str
    deployment_id: str
    token_sha256: str
    tls_cert_pem: str
    tls_key_pem: str
    listen_host: str = "0.0.0.0"
    listen_port: int = 443
    engine: EngineName = "comfy"
    models_dir: str = "/var/lib/aivo/models"
    volume_mount: str | None = None
    job_dir: str = "/run/aivo/jobs"  # tmpfs: inputs/outputs never touch disk
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    worker_image: str | None = None


# ---------------------------------------------------------------- files


class FileSource(BaseModel):
    kind: Literal["volume", "url", "hf"]
    url: str | None = None
    path: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class FileFetch(BaseModel):
    dest: str  # relative to models_dir, e.g. "diffusion_models/z_image_turbo_bf16.safetensors"
    bytes: int | None = None
    sha256: str | None = None
    sources: list[FileSource] = Field(default_factory=list)


class EnsureFilesIn(BaseModel):
    files: list[FileFetch]


class FileState(BaseModel):
    dest: str
    state: Literal["pending", "downloading", "ready", "failed"] = "pending"
    bytes_done: int = 0
    bytes_total: int | None = None
    source: str | None = None
    seconds: float | None = None
    error: str | None = None


class FilesStatus(BaseModel):
    files: list[FileState]

    @property
    def all_ready(self) -> bool:
        return all(f.state == "ready" for f in self.files)

    @property
    def failed(self) -> list[FileState]:
        return [f for f in self.files if f.state == "failed"]


# ---------------------------------------------------------------- jobs


class OutputSpec(BaseModel):
    kind: OutputKind
    count: int = 1
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    fps: int | None = None
    with_audio: bool = False


class JobIn(BaseModel):
    job_id: str
    model_id: str
    workflow: dict = Field(default_factory=dict)
    inputs: list[str] = Field(default_factory=list)  # names previously uploaded to /v1/inputs
    required_files: list[str] = Field(default_factory=list)  # dests that must be ready
    output: OutputSpec
    timeout_s: int = 3600


class JobState(StrEnum):
    queued = "queued"
    running = "running"
    postprocessing = "postprocessing"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (JobState.succeeded, JobState.failed, JobState.cancelled)


class OutputMeta(BaseModel):
    name: str
    media_type: str
    bytes: int
    sha256: str
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None


class JobStatus(BaseModel):
    job_id: str
    state: JobState
    progress: float = 0.0
    stage: str = ""
    eta_s: float | None = None
    outputs: list[OutputMeta] = Field(default_factory=list)
    error: str | None = None
    error_code: Literal["oom", "engine", "timeout", "cancelled", "missing_files", "bad_request"] | None = None


# ---------------------------------------------------------------- health


class GpuInfo(BaseModel):
    name: str
    vram_total_gb: float
    vram_free_gb: float | None = None


class Health(BaseModel):
    protocol: str = PROTOCOL_VERSION
    agent_version: str
    instance_id: str
    engine: EngineName
    engine_ready: bool
    gpu: GpuInfo | None = None
    disk_free_gb: float | None = None
    files_present: list[str] = Field(default_factory=list)
    models_loaded: list[str] = Field(default_factory=list)
    running_job: str | None = None
    outputs_pending: int = 0
    draining: bool = False
    uptime_s: float = 0.0
    image_digest: str | None = None


class RotateTokenOut(BaseModel):
    token: str


class PhaseTiming(BaseModel):
    phase: str
    seconds: float
    source: str | None = None
    bytes: int | None = None
    model_id: str | None = None


class TelemetryOut(BaseModel):
    phases: list[PhaseTiming] = Field(default_factory=list)
