"""What aivo needs from RunPod, behind one interface (real: HTTP; development/CI: ``fake.FakeRunPod``).

* REST API v2 (``https://api.runpod.io/v2``): GPU catalog, queue endpoints (one per GPU type), network volume.
* Queue API (``https://api.runpod.ai/v2/<endpoint>``): submit a job (``/run``), its ``/status``, ``/cancel``,
  and the endpoint's ``/health`` (jobs waiting / workers running).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx

from aivo.providers.runpod.client import RunPodClient


@runtime_checkable
class RunPodAPI(Protocol):
    name: str

    async def gpu_types(self, params: dict[str, Any]) -> list[dict[str, Any]]: ...

    async def data_centers(self) -> list[dict[str, Any]]: ...

    async def network_volume(self, volume_id: str) -> dict[str, Any] | None: ...

    async def list_endpoints(self) -> list[dict[str, Any]]: ...

    async def get_endpoint(self, endpoint_id: str) -> dict[str, Any] | None: ...

    async def create_endpoint(self, body: dict[str, Any]) -> dict[str, Any]: ...

    async def update_endpoint(self, endpoint_id: str, body: dict[str, Any]) -> dict[str, Any]: ...

    async def delete_endpoint(self, endpoint_id: str) -> None: ...

    async def run(self, endpoint_id: str, job_input: dict[str, Any], policy: dict[str, Any]) -> str: ...

    async def status(self, endpoint_id: str, job_id: str) -> dict[str, Any] | None: ...

    async def cancel(self, endpoint_id: str, job_id: str) -> None: ...

    async def health(self, endpoint_id: str) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class HttpRunPod:
    name = "runpod"

    def __init__(
        self,
        api_key: str,
        *,
        api_base: str = "https://api.runpod.io",
        queue_base: str = "https://api.runpod.ai",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.rest = RunPodClient(api_key, api_base, transport=transport)
        self.queue = RunPodClient(api_key, queue_base, transport=transport)

    async def aclose(self) -> None:
        await self.rest.aclose()
        await self.queue.aclose()

    # -- REST API v2 -----------------------------------------------------------------

    async def gpu_types(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        data = await self.rest.request("GET", "/v2/catalog/gpus", params=params or None) or {}
        return list(data.get("gpus") or [])

    async def data_centers(self) -> list[dict[str, Any]]:
        data = await self.rest.request("GET", "/v2/catalog/datacenters") or {}
        return list(data.get("dataCenters") or [])

    async def network_volume(self, volume_id: str) -> dict[str, Any] | None:
        return await self.rest.request("GET", f"/v2/network-volumes/{volume_id}", missing_ok=True)

    async def list_endpoints(self) -> list[dict[str, Any]]:
        return await self.rest.paginate("/v2/serverless", "endpoints")

    async def get_endpoint(self, endpoint_id: str) -> dict[str, Any] | None:
        return await self.rest.request("GET", f"/v2/serverless/{endpoint_id}", missing_ok=True)

    async def create_endpoint(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.rest.request("POST", "/v2/serverless", json=body, creating=True) or {}

    async def update_endpoint(self, endpoint_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.rest.request("PATCH", f"/v2/serverless/{endpoint_id}", json=body) or {}

    async def delete_endpoint(self, endpoint_id: str) -> None:
        await self.rest.request("DELETE", f"/v2/serverless/{endpoint_id}", missing_ok=True)

    # -- queue -----------------------------------------------------------------------

    async def run(self, endpoint_id: str, job_input: dict[str, Any], policy: dict[str, Any]) -> str:
        data = await self.queue.request(
            "POST", f"/v2/{endpoint_id}/run", json={"input": job_input, "policy": policy}, creating=True
        )
        if not data or not data.get("id"):
            raise RuntimeError(f"RunPod did not return a job id: {data}")
        return str(data["id"])

    async def status(self, endpoint_id: str, job_id: str) -> dict[str, Any] | None:
        return await self.queue.request("GET", f"/v2/{endpoint_id}/status/{job_id}", missing_ok=True)

    async def cancel(self, endpoint_id: str, job_id: str) -> None:
        await self.queue.request("POST", f"/v2/{endpoint_id}/cancel/{job_id}", missing_ok=True)

    async def health(self, endpoint_id: str) -> dict[str, Any]:
        return await self.queue.request("GET", f"/v2/{endpoint_id}/health", missing_ok=True) or {}
