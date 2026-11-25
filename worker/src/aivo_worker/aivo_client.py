"""The worker's side of the push channel: it calls aivo, never the other way round.

Every call carries the job's one-time ticket token. Calls are retried on network errors, because a worker
that loses a finished result would have to redo the whole creation.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

import httpx

from aivo_protocol.worker_api import (
    CompleteIn,
    FailIn,
    ProgressIn,
    ProgressOut,
    PromptIn,
    PromptOut,
    Ticket,
    UploadOut,
    WorkerInfo,
    WorkSpec,
)

log = logging.getLogger(__name__)

RETRIES = 4


class TicketRejected(Exception):
    """aivo no longer wants this job (cancelled, finished elsewhere, or the ticket expired)."""


class AivoClient:
    def __init__(self, ticket: Ticket, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 60):
        self.ticket = ticket
        self.base = f"{ticket.aivo_url.rstrip('/')}/api/v1/worker/jobs/{ticket.job_id}"
        self._c = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {ticket.token}", "X-Requested-With": "aivo"},
            timeout=httpx.Timeout(timeout, connect=15),
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> AivoClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._c.aclose()

    async def _call(self, method: str, path: str, *, retries: int = RETRIES, **kw: Any) -> httpx.Response:
        body_factory = kw.pop("body_factory", None)  # a streamed body must be recreated for each attempt
        for attempt in range(retries):
            if body_factory is not None:
                kw["content"] = body_factory()
            try:
                r = await self._c.request(method, self.base + path, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt == retries - 1:
                    raise
                log.warning("aivo unreachable (%s), retrying", e)
                await asyncio.sleep(2**attempt)
                continue
            if r.status_code in (401, 404, 409, 410):
                raise TicketRejected(f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code >= 500 and attempt < retries - 1:
                await asyncio.sleep(2**attempt)
                continue
            r.raise_for_status()
            return r
        raise RuntimeError("unreachable")

    async def spec(self, worker: WorkerInfo) -> WorkSpec:
        r = await self._call("POST", "/spec", json=worker.model_dump())
        return WorkSpec.model_validate(r.json())

    async def prompt(self, body: PromptIn) -> PromptOut:
        r = await self._call("POST", "/prompt", json=body.model_dump(), retries=2)
        return PromptOut.model_validate(r.json())

    async def get_input(self, name: str, dest: Path) -> int:
        r = await self._call("GET", f"/inputs/{name}")
        dest.write_bytes(r.content)
        return len(r.content)

    async def progress(self, body: ProgressIn) -> ProgressOut:
        """Best effort: progress is nice to have, the creation goes on without it."""
        try:
            r = await self._call("POST", "/progress", json=body.model_dump(), retries=2)
            return ProgressOut.model_validate(r.json())
        except TicketRejected:
            raise
        except Exception as e:
            log.info("progress update not delivered: %s", e)
            return ProgressOut()

    async def upload(self, name: str, path: Path, media_type: str) -> UploadOut:
        digest = await asyncio.to_thread(_sha256, path)

        async def body():  # type: ignore[no-untyped-def]
            with path.open("rb") as fh:
                while chunk := fh.read(1 << 20):
                    yield chunk

        r = await self._call(
            "PUT",
            f"/outputs/{name}",
            params={"media_type": media_type, "sha256": digest},
            body_factory=body,
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size)},
            timeout=httpx.Timeout(600, connect=15),
        )
        out = UploadOut.model_validate(r.json())
        if out.sha256 != digest:
            raise RuntimeError(f"aivo received a different file for {name}")
        return out

    async def complete(self, body: CompleteIn) -> None:
        await self._call("POST", "/complete", json=body.model_dump())

    async def fail(self, body: FailIn) -> None:
        try:
            await self._call("POST", "/fail", json=body.model_dump())
        except TicketRejected:
            pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()
