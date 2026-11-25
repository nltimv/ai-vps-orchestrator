from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from aivo_protocol.worker_api import GpuInfo, JobIn

ProgressCb = Callable[[float, str], None]
OutputsCb = Callable[[list[Path]], Awaitable[None]]  # results as they're done (each batch)


class EngineError(Exception):
    def __init__(self, message: str, code: str = "engine"):
        super().__init__(message)
        self.code = code


class Engine(Protocol):
    name: str

    @property
    def ready(self) -> bool: ...

    @property
    def models_loaded(self) -> list[str]: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def run(
        self,
        job: JobIn,
        inputs_dir: Path,
        out_dir: Path,
        progress: ProgressCb,
        on_outputs: OutputsCb | None = None,
    ) -> list[Path]: ...

    async def generate_text(self, workflow: dict[str, Any], output_node: str) -> str:
        """Run a workflow that writes text (a language model) and return what ``output_node`` shows."""
        ...

    async def cancel(self) -> None: ...

    def gpu(self) -> GpuInfo | None: ...
