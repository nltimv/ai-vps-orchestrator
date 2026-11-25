"""Wire contract between the backend and the GPU worker (RunPod Serverless, queue-based endpoints).

aivo submits a job to the queue endpoint of a GPU type. The job's whole input is a ``Ticket``: aivo's
address, the aivo job id and a one-time token. The worker then talks to aivo directly over HTTPS:

    POST /api/v1/worker/jobs/{id}/spec        -> WorkSpec   (what to run; marks the job "preparing")
    GET  /api/v1/worker/jobs/{id}/inputs/{n}  -> bytes      (starting picture)
    POST /api/v1/worker/jobs/{id}/prompt      -> PromptOut  (the rewritten description, checked; protocol 2.1)
    POST /api/v1/worker/jobs/{id}/progress    -> ProgressOut (progress + log lines; says when to cancel)
    PUT  /api/v1/worker/jobs/{id}/outputs/{n} -> UploadOut  (each finished file, streamed)
    POST /api/v1/worker/jobs/{id}/complete | /fail

So prompts, starting pictures and results never pass through RunPod's job system, and there is no size limit
on results. The ticket stops working when the job ends.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from aivo_protocol.version import PROTOCOL_VERSION

OutputKind = Literal["image", "video", "audio"]
ErrorCode = Literal["oom", "engine", "timeout", "cancelled", "missing_files", "bad_request"]


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
    inputs: list[str] = Field(default_factory=list)  # names of starting files in the inputs directory
    required_files: list[str] = Field(default_factory=list)  # dests that must be ready
    more: list[dict] = Field(default_factory=list)  # further runs (next batches), one after another
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
    error_code: ErrorCode | None = None


# ---------------------------------------------------------------- worker <-> aivo


class GpuInfo(BaseModel):
    name: str
    vram_total_gb: float
    vram_free_gb: float | None = None


class LogLine(BaseModel):
    seq: int
    ts: float
    source: str
    level: str | None = None
    text: str


class LogsOut(BaseModel):
    lines: list[LogLine] = Field(default_factory=list)
    next_seq: int = 0  # pass back as ?since= to get only newer lines
    dropped: int = 0  # lines that were evicted before the caller saw them
    reset: bool = False  # numbering began again


class PhaseTiming(BaseModel):
    phase: str
    seconds: float
    source: str | None = None
    bytes: int | None = None
    model_id: str | None = None


class Ticket(BaseModel):
    """The whole input of a RunPod job. Holds no prompt or picture: the worker fetches those from aivo."""

    aivo_url: str
    job_id: str
    token: str
    protocol: str = PROTOCOL_VERSION


class WorkerInfo(BaseModel):
    """Who is working, for billing: RunPod bills a worker from its start until it scales down."""

    worker_id: str  # RunPod's id of the worker (RUNPOD_POD_ID)
    endpoint_id: str | None = None
    started_at: float  # unix time the worker process started
    job_started_at: float | None = None  # unix time this job reached the worker
    prev_job_end: float | None = None  # unix time this worker finished its previous job
    jobs_done: int = 0
    gpu: GpuInfo | None = None
    agent_version: str = "dev"
    protocol: str = PROTOCOL_VERSION


class RewriteSpec(BaseModel):
    """Improve the description on the GPU before creating (protocol 2.1).

    The worker runs ``workflow`` (a small language model in ComfyUI), reads the text that ``output_node``
    shows, and sends it to aivo (``/prompt``), which checks it and answers with the description to use.
    That replaces ``marker`` inside the string values of ``WorkSpec.workflow``. If anything goes wrong
    the worker uses ``fallback``: the description as the user wrote it.
    """

    workflow: dict = Field(default_factory=dict)
    output_node: str
    marker: str
    fallback: str
    files: list[str] = Field(default_factory=list)  # dests of WorkSpec.files the language model needs
    timeout_s: int = 180
    # protocol 2.3: a description per further run (WorkSpec.more), each steered differently, so the results
    # of one creation differ; runs without one use the first
    more: list[dict] = Field(default_factory=list)


class WorkSpec(BaseModel):
    job_id: str
    model_id: str
    workflow: dict = Field(default_factory=dict)
    files: list[FileFetch] = Field(default_factory=list)  # model files, with where to get them
    inputs: list[str] = Field(default_factory=list)  # starting files to fetch from aivo
    output: OutputSpec
    timeout_s: int = 3600  # for all runs together
    rewrite: RewriteSpec | None = None
    # protocol 2.2: further runs of the same workflow (the next batches of a creation with more pictures or
    # videos than one run makes), each with its own seed; their outputs are numbered on after the first's
    more: list[dict] = Field(default_factory=list)


class PromptIn(BaseModel):
    index: int = 0  # which run it's for: 0 = WorkSpec.workflow, 1 = WorkSpec.more[0], ...
    text: str | None = None  # what the language model wrote; None if it failed
    error: str | None = None
    seconds: float | None = None


class PromptOut(BaseModel):
    prompt: str  # the description to create with
    rewritten: bool  # False: aivo kept the user's own description


class ProgressIn(BaseModel):
    stage: str  # loading_model | generating | saving | ...
    progress: float = 0.0
    eta_s: float | None = None
    lines: list[LogLine] = Field(default_factory=list)


class ProgressOut(BaseModel):
    cancel: bool = False


class UploadOut(BaseModel):
    bytes: int
    sha256: str


class CompleteIn(BaseModel):
    outputs: list[OutputMeta]
    phases: list[PhaseTiming] = Field(default_factory=list)
    worker: WorkerInfo
    finished_at: float
    lines: list[LogLine] = Field(default_factory=list)


class FailIn(BaseModel):
    code: ErrorCode
    message: str
    worker: WorkerInfo | None = None
    finished_at: float | None = None
    lines: list[LogLine] = Field(default_factory=list)
