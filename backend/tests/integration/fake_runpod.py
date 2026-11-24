"""A local stand-in for RunPod Serverless: the REST API v2 and the load-balancing gateway in one HTTP server.

Each endpoint gets at most one worker: a real aivo agent (mock engine, plain HTTP) started on the first
gateway request, exactly as RunPod starts a worker on demand. The gateway enforces what RunPod documents
and aivo relies on: the API key, ``X-Runpod-Worker-Id`` (strict affinity -> 404 affinity_worker_gone),
and the 30 MB request/response limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from aivo_worker.engines.mock import MockEngine
from aivo_worker.main import load_bootstrap
from aivo_worker.server import WorkerServer, build_state

CATALOG = [
    {"id": "NVIDIA RTX A5000", "name": "RTX A5000", "pool": "AMPERE_24", "memory": 24, "price": 0.68, "ram": 46},
    {"id": "NVIDIA L4", "name": "L4", "pool": "AMPERE_24", "memory": 24, "price": 0.68, "ram": 50},
    {"id": "NVIDIA L40S", "name": "L40S", "pool": "ADA_48_PRO", "memory": 48, "price": 1.91, "ram": 94},
    {
        "id": "NVIDIA RTX 6000 Ada Generation",
        "name": "RTX 6000 Ada",
        "pool": "ADA_48_PRO",
        "memory": 48,
        "price": 1.91,
        "ram": 167,
    },
    {"id": "NVIDIA H100 80GB HBM3", "name": "H100 SXM", "pool": "ADA_80_PRO", "memory": 80, "price": 4.18, "ram": 188},
]


@dataclass
class FakeEndpoint:
    body: dict[str, Any]
    id: str
    created: float = field(default_factory=time.monotonic)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    worker_id: str | None = None
    server: WorkerServer | None = None
    last_request: float = 0.0
    first_request_at: float | None = None
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    output_range_requests: int = 0

    def as_json(self) -> dict[str, Any]:
        return {**self.body, "id": self.id, "createdAt": self.created_at, "requestUrls": {}}


class FakeRunPod:
    def __init__(
        self,
        root: Path,
        *,
        api_key: str = "rp-test-key",
        prepare_s: float = 0.3,
        job_seconds: float = 0.3,
        max_workers: int = 5,
        max_body: int = 30 * 1024 * 1024,
        kill_after_submit: int = 0,
        port: int = 0,
    ):
        self.root = root
        self.api_key = api_key
        self.prepare_s = prepare_s
        self.job_seconds = job_seconds
        self.max_workers = max_workers
        self.max_body = max_body
        self.endpoints: dict[str, FakeEndpoint] = {}
        self.deleted: list[str] = []
        self.created: list[dict[str, Any]] = []
        self.unhealthy = False  # workers fail their health check
        self.port = port
        self.kill_after_submit = kill_after_submit  # the worker vanishes right after this many job submissions
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self.base = ""
        self._http = httpx.AsyncClient(timeout=60, trust_env=False)
        self.app = self._make_app()

    # -- lifecycle ----------------------------------------------------------------------

    async def __aenter__(self) -> FakeRunPod:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        self.base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self._server = uvicorn.Server(uvicorn.Config(self.app, log_level="warning", lifespan="off"))
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        for _ in range(200):
            if self._server.started:
                break
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc: object) -> None:
        for ep in list(self.endpoints.values()):
            await self._stop_worker(ep)
        if self._server:
            self._server.should_exit = True
        if self._task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, 5)
        await self._http.aclose()

    @property
    def gateway_url(self) -> str:
        return self.base + "/gw/{endpoint_id}"

    def env_overrides(self) -> dict[str, Any]:
        return {
            "provider": "runpod",
            "runpod_api_base": self.base,
            "runpod_gateway_url": self.gateway_url,
            "runpod_countries": ["NL"],
        }

    # -- workers ------------------------------------------------------------------------

    def _status(self, ep: FakeEndpoint) -> str:
        if self.unhealthy:
            return "UNHEALTHY"
        if ep.server is not None:
            return "RUNNING"
        if time.monotonic() - ep.created < self.prepare_s:
            return "INITIALIZING"
        return "IDLE"

    async def _start_worker(self, ep: FakeEndpoint) -> None:
        boot = load_bootstrap(Path("/nonexistent"), environ=ep.body["env"])
        root = self.root / ep.id / secrets.token_hex(3)
        boot = boot.model_copy(
            update={"models_dir": str(root / "models"), "job_dir": str(root / "jobs"), "engine": "mock"}
        )
        assert not boot.tls_cert_pem and not boot.tls_key_pem, "no TLS material may travel to RunPod"
        state = build_state(boot, MockEngine(self.job_seconds), simulate_files=True)
        ep.server = WorkerServer(state, host="127.0.0.1", port=0)
        await ep.server.start()
        ep.worker_id = f"wrk-{secrets.token_hex(4)}"

    async def _stop_worker(self, ep: FakeEndpoint) -> None:
        srv, ep.server, ep.worker_id = ep.server, None, None
        if srv is not None:
            await srv.stop()

    async def scale_down(self, endpoint_id: str) -> None:
        await self._stop_worker(self.endpoints[endpoint_id])

    # -- app ----------------------------------------------------------------------------

    def _make_app(self) -> FastAPI:
        app = FastAPI()

        def auth(request: Request) -> None:
            if request.headers.get("authorization") != f"Bearer {self.api_key}":
                raise HTTPException(401, "bad api key")

        @app.get("/v2/catalog/gpus")
        async def gpus(request: Request) -> dict[str, Any]:
            auth(request)
            assert request.query_params.get("product") == "SERVERLESS"
            out = []
            for g in CATALOG:
                out.append(
                    {
                        "id": g["id"],
                        "name": g["name"],
                        "pool": g["pool"],
                        "manufacturer": "NVIDIA",
                        "memory": g["memory"],
                        "secure": True,
                        "community": False,
                        "price": {"secure": g["price"] / 1.7, "community": 0, "serverless": g["price"]},
                        "maxCount": {"secure": 8, "community": 0},
                        "minRam": g["ram"],
                        "availability": "HIGH",
                        "dataCenters": [{"id": "EU-NL-1", "name": "Netherlands 1", "availability": "HIGH"}],
                    }
                )
            return {"gpus": out}

        @app.post("/v2/serverless", status_code=201)
        async def create(request: Request) -> Any:
            auth(request)
            body = await request.json()
            used = sum(e.body["workers"]["max"] for e in self.endpoints.values())
            if used + body["workers"]["max"] > self.max_workers:
                return JSONResponse(
                    {"title": "Bad Request", "status": 400, "detail": "max workers limit exceeded for your account"},
                    status_code=400,
                )
            assert body["type"] == "LOAD_BALANCER"
            assert body["scaling"]["type"] == "REQUEST_COUNT"
            eid = secrets.token_hex(4)
            ep = FakeEndpoint(body=body, id=eid)
            self.endpoints[eid] = ep
            self.created.append(body)
            return ep.as_json()

        @app.get("/v2/serverless")
        async def list_endpoints(request: Request) -> dict[str, Any]:
            auth(request)
            return {
                "endpoints": [e.as_json() for e in self.endpoints.values()],
                "pagination": {"nextCursor": None, "hasNextPage": False},
            }

        @app.get("/v2/serverless/{eid}")
        async def get_endpoint(eid: str, request: Request) -> Any:
            auth(request)
            ep = self.endpoints.get(eid)
            if ep is None:
                return JSONResponse({"title": "Not Found", "status": 404, "detail": "not found"}, status_code=404)
            return ep.as_json()

        @app.get("/v2/serverless/{eid}/workers")
        async def workers(eid: str, request: Request) -> Any:
            auth(request)
            ep = self.endpoints.get(eid)
            if ep is None:
                return JSONResponse({"title": "Not Found", "status": 404, "detail": "not found"}, status_code=404)
            st = self._status(ep)
            summary = {"running": 0, "idle": 0, "initializing": 0, "throttled": 0, "unhealthy": 0, "total": 1}
            summary[st.lower()] = 1
            ws = [{"id": ep.worker_id or "wrk-pending", "status": st, "gpuCount": 1, "isStale": False}]
            if st != "RUNNING":  # scaled-down workers aren't listed, only counted
                ws = []
            return {"workers": ws, "summary": summary}

        @app.delete("/v2/serverless/{eid}", status_code=204)
        async def delete(eid: str, request: Request) -> Response:
            auth(request)
            ep = self.endpoints.pop(eid, None)
            if ep is None:
                return JSONResponse({"title": "Not Found", "status": 404, "detail": "not found"}, status_code=404)
            await self._stop_worker(ep)
            self.deleted.append(eid)
            return Response(status_code=204)

        @app.api_route("/gw/{eid}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
        async def gateway(eid: str, path: str, request: Request) -> Response:
            if request.headers.get("authorization") != f"Bearer {self.api_key}":
                return Response("unauthorized", status_code=401)
            ep = self.endpoints.get(eid)
            if ep is None:
                return Response("endpoint not found", status_code=404)
            affinity = request.headers.get("x-runpod-worker-id", "")
            mode, _, wid = affinity.partition(" ")
            if mode in ("strict", "strict-resume") and wid != ep.worker_id:
                return JSONResponse({"reason": "affinity_worker_gone"}, status_code=404)
            if self.unhealthy:
                return Response("no workers available", status_code=502)
            if ep.server is None:
                if time.monotonic() - ep.created < self.prepare_s:
                    await asyncio.sleep(self.prepare_s)
                await self._start_worker(ep)  # cold start: billing begins
            if ep.first_request_at is None:
                ep.first_request_at = time.monotonic()
            ep.last_request = time.monotonic()
            body = await request.body()
            if len(body) > self.max_body:
                return Response("request body too large", status_code=413)
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() not in ("host", "content-length", "x-runpod-worker-id")
            }
            ep.requests.append((request.method, "/" + path, headers))
            if "/outputs/" in path:
                ep.output_range_requests += 1
            assert ep.server is not None
            url = f"http://127.0.0.1:{ep.server.port}/{path}"
            r = await self._http.request(
                request.method, url, params=dict(request.query_params), content=body, headers=headers
            )
            if len(r.content) > self.max_body:
                return Response("response too large", status_code=502)
            out_headers = {
                k: v for k, v in r.headers.items() if k.lower() in ("content-type", "content-range", "accept-ranges")
            }
            out_headers["X-Runpod-Worker-Id"] = ep.worker_id or ""
            resp = Response(r.content, status_code=r.status_code, headers=out_headers)
            if request.method == "POST" and path == "v1/jobs" and r.status_code == 200 and self.kill_after_submit:
                self.kill_after_submit -= 1
                if self.kill_after_submit == 0:
                    await self._stop_worker(ep)  # the worker vanishes mid-job
            return resp

        return app
