"""HTTPS client for a worker agent: pinned to that machine's ephemeral CA, or through RunPod's gateway.

Gateway mode (RunPod Serverless): ``Authorization`` carries the RunPod API key, the worker token goes in
``X-Aivo-Token``, and after the first contact every request is pinned to that one worker
(``X-Runpod-Worker-Id: strict <id>``), so a replaced worker shows up as "worker gone" instead of
silently losing the job. Bodies are capped at 30 MB per request, so outputs are fetched in ranges.
"""

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


WORKER_ID_HEADER = "X-Runpod-Worker-Id"
GATEWAY_MAX_BODY = 24 * 1024 * 1024  # RunPod: 30 MB per request and response
DOWNLOAD_CHUNK = 16 * 1024 * 1024


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
        self.sni: str | None = sni_name
        self.gateway = False
        self.worker_id: str | None = None
        self.max_body: int | None = None

    @classmethod
    def via_gateway(
        cls,
        base_url: str,
        token: str,
        *,
        api_key: str,
        worker_id: str | None = None,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> WorkerClient:
        """A worker behind RunPod's load-balancing gateway (it terminates HTTPS with a public certificate)."""
        self = cls.__new__(cls)
        self.base = base_url.rstrip("/")
        self.sni = None
        self.gateway = True
        self.worker_id = worker_id
        self.max_body = GATEWAY_MAX_BODY
        headers = {"Authorization": f"Bearer {api_key}", "X-Aivo-Token": token}
        if worker_id:
            headers[WORKER_ID_HEADER] = f"strict {worker_id}"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10), headers=headers, transport=transport
        )
        return self

    async def __aenter__(self) -> WorkerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _ext(self) -> dict[str, Any]:
        return {"sni_hostname": self.sni} if self.sni else {}

    async def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        ext = {**kw.pop("extensions", {}), **self._ext()}
        try:
            r = await self._client.request(method, self.base + path, extensions=ext, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            raise WorkerUnreachable(str(e)) from e
        self._check_gateway(r)
        if r.status_code == 401:
            raise WorkerUnreachable("worker rejected credentials")
        return r

    def _check_gateway(self, r: httpx.Response) -> None:
        """RunPod's gateway answers for the worker when the worker can't (gone, starting, overloaded)."""
        if not self.gateway:
            return
        wid = r.headers.get(WORKER_ID_HEADER)
        if wid and not self.worker_id:
            self.worker_id = wid
        if r.status_code in (400, 404) and ("affinity_worker_gone" in r.text or "worker_timeout" in r.text):
            raise WorkerUnreachable(
                "the RunPod worker is gone" if r.status_code == 404 else "the RunPod worker is busy"
            )
        if r.status_code in (502, 503, 504) or (r.status_code == 401 and not wid):
            # no worker answered: still starting, scaled down, or the RunPod key was refused
            raise WorkerUnreachable(f"RunPod gateway: HTTP {r.status_code} {r.text[:200]}")

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
        if self.max_body is not None and len(data) > self.max_body:
            raise ValueError(f"the starting file is {len(data) / 1e6:.0f} MB; RunPod accepts at most 24 MB")
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
        if self.max_body is not None:
            return await self._download_ranges(job_id, name, dest)
        ext = self._ext()
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

    async def _download_ranges(self, job_id: str, name: str, dest: Path) -> int:
        """In pieces below the gateway's body limit (each piece is its own short request)."""
        chunk = min(DOWNLOAD_CHUNK, self.max_body or DOWNLOAD_CHUNK)
        total: int | None = None
        done = 0
        with dest.open("wb") as fh:
            while total is None or done < total:
                r = await self._req(
                    "GET",
                    f"/v1/jobs/{job_id}/outputs/{name}",
                    headers={"Range": f"bytes={done}-{done + chunk - 1}"},
                    timeout=300,
                )
                if r.status_code == 416 and total is None and done == 0:
                    return 0  # empty file
                r.raise_for_status()
                if r.status_code == 200:  # the worker ignored the range: it was small enough to come whole
                    fh.write(r.content)
                    return len(r.content)
                cr = r.headers.get("content-range", "")
                if "/" not in cr or not cr.rsplit("/", 1)[1].isdigit():
                    raise WorkerUnreachable(f"bad range answer for {name}")
                total = int(cr.rsplit("/", 1)[1])
                if not r.content:
                    raise WorkerUnreachable(f"empty range answer for {name}")
                fh.write(r.content)
                done += len(r.content)
        return done

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
        if self.gateway:  # Authorization carries the RunPod key there
            self._client.headers["X-Aivo-Token"] = token
        else:
            self._client.headers["Authorization"] = f"Bearer {token}"
        return token

    async def telemetry(self) -> TelemetryOut:
        r = await self._req("GET", "/v1/telemetry")
        r.raise_for_status()
        return TelemetryOut.model_validate(r.json())
