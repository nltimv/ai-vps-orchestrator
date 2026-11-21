"""HTTPS client for a worker agent, pinned to that machine's ephemeral CA."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import httpx

from aivo_protocol.worker_api import (
    BootStatus,
    EnsureFilesIn,
    FilesStatus,
    Health,
    JobIn,
    JobStatus,
    LogsOut,
    RotateTokenOut,
    TelemetryOut,
)


class WorkerUnreachable(Exception):
    pass


class WorkerClient:
    def __init__(self, host: str, port: int, ca_pem: str, sni_name: str, token: str, *, timeout: float = 30):
        ctx = ssl.create_default_context(cadata=ca_pem)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        h = f"[{host}]" if ":" in host else host
        self.base = f"https://{h}:{port}"
        self.sni = sni_name
        self._client = httpx.AsyncClient(
            verify=ctx,
            timeout=httpx.Timeout(timeout, connect=10),
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,  # never route worker traffic through a proxy
        )

    async def __aenter__(self) -> WorkerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        ext = kw.pop("extensions", {})
        ext["sni_hostname"] = self.sni
        try:
            r = await self._client.request(method, self.base + path, extensions=ext, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            raise WorkerUnreachable(str(e)) from e
        if r.status_code == 401:
            raise WorkerUnreachable("worker rejected credentials")
        return r

    async def health(self) -> Health:
        r = await self._req("GET", "/v1/health", timeout=10)
        r.raise_for_status()
        return Health.model_validate(r.json())

    async def probe(self) -> Health | BootStatus:
        """Health of the agent, or, while the machine is still setting up, its start-up status."""
        r = await self._req("GET", "/v1/health", timeout=10)
        if r.status_code == 503:
            try:
                body = r.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and isinstance(body.get("bootstrap"), dict):
                return BootStatus.model_validate(body["bootstrap"])
        r.raise_for_status()
        return Health.model_validate(r.json())

    async def logs(self, since: int = 0, *, wait: float = 0, tail: int | None = None, limit: int = 1000) -> LogsOut:
        params: dict[str, Any] = {"tail": tail} if tail is not None else {"since": since, "wait": wait, "limit": limit}
        r = await self._req("GET", "/v1/logs", params=params, timeout=wait + 15)
        if r.status_code == 404:  # older worker software without a log endpoint
            return LogsOut(next_seq=since)
        r.raise_for_status()
        return LogsOut.model_validate(r.json())

    async def ensure_files(self, body: EnsureFilesIn) -> FilesStatus:
        r = await self._req("POST", "/v1/files/ensure", json=body.model_dump())
        r.raise_for_status()
        return FilesStatus.model_validate(r.json())

    async def files_status(self, dests: list[str]) -> FilesStatus:
        r = await self._req("GET", "/v1/files/status", params=[("dest", d) for d in dests])
        r.raise_for_status()
        return FilesStatus.model_validate(r.json())

    async def put_input(self, name: str, data: bytes) -> None:
        r = await self._req("PUT", f"/v1/inputs/{name}", content=data)
        r.raise_for_status()

    async def submit(self, job: JobIn) -> JobStatus:
        r = await self._req("POST", "/v1/jobs", json=job.model_dump())
        if r.status_code == 409:
            raise WorkerUnreachable("worker is draining")
        r.raise_for_status()
        return JobStatus.model_validate(r.json())

    async def job(self, job_id: str, wait: float = 0) -> JobStatus | None:
        r = await self._req("GET", f"/v1/jobs/{job_id}", params={"wait": wait}, timeout=wait + 15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return JobStatus.model_validate(r.json())

    async def cancel(self, job_id: str) -> None:
        await self._req("POST", f"/v1/jobs/{job_id}/cancel")

    async def download(self, job_id: str, name: str, dest: Path) -> int:
        ext = {"sni_hostname": self.sni}
        size = 0
        try:
            async with self._client.stream(
                "GET", f"{self.base}/v1/jobs/{job_id}/outputs/{name}", extensions=ext, timeout=300
            ) as r:
                r.raise_for_status()
                with dest.open("wb") as fh:
                    async for chunk in r.aiter_bytes(1 << 20):
                        fh.write(chunk)
                        size += len(chunk)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            raise WorkerUnreachable(str(e)) from e
        return size

    async def delete_job(self, job_id: str) -> None:
        await self._req("DELETE", f"/v1/jobs/{job_id}")

    async def drain(self) -> Health:
        r = await self._req("POST", "/v1/drain")
        r.raise_for_status()
        return Health.model_validate(r.json())

    async def rotate_token(self) -> str:
        r = await self._req("POST", "/v1/rotate-token")
        r.raise_for_status()
        token = RotateTokenOut.model_validate(r.json()).token
        self._client.headers["Authorization"] = f"Bearer {token}"
        return token

    async def telemetry(self) -> TelemetryOut:
        r = await self._req("GET", "/v1/telemetry")
        r.raise_for_status()
        return TelemetryOut.model_validate(r.json())
