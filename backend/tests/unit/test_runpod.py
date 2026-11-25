"""RunPod pieces: the HTTP client (paths, errors), the endpoint aivo creates per GPU type, worker start-up."""

from __future__ import annotations

import collections
import json
import uuid

import httpx
import pytest

from aivo.config import EnvSettings, RuntimeSettings
from aivo.controller.endpoints import EndpointManager
from aivo.providers.base import AuthError, ConfigError, QuotaError, RateLimited, UnknownOutcome
from aivo.providers.runpod.api import HttpRunPod
from aivo.providers.runpod.catalog import GpuCatalog, gpu_class_of, gpu_slug
from aivo.providers.runpod.client import classify
from aivo.providers.runpod.fake import FakeRunPod


@pytest.mark.parametrize(
    ("status", "body", "creating", "kind"),
    [
        (401, {"title": "Unauthorized"}, False, AuthError),
        (429, {"title": "Too Many Requests"}, False, RateLimited),
        (502, "bad gateway", True, UnknownOutcome),
        (400, {"detail": "max workers limit exceeded for your account"}, True, QuotaError),
        (400, {"detail": "insufficient balance"}, True, QuotaError),
        (422, {"detail": "invalid image reference"}, True, ConfigError),
    ],
)
def test_api_errors_are_classified(status, body, creating, kind):
    assert isinstance(classify(status, body, creating=creating), kind)


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        ("NVIDIA L40S", "l40s"),
        ("NVIDIA L40", "l40"),
        ("NVIDIA RTX 6000 Ada Generation", "rtx6000ada"),
        ("NVIDIA RTX PRO 6000 Blackwell Server Edition", "rtxpro6000"),
        ("NVIDIA H100 80GB HBM3", "h100"),
        ("NVIDIA GeForce RTX 4090", "rtx4090"),
        ("NVIDIA RTX A5000", "a5000"),
        ("NVIDIA L4", "l4"),
        ("NVIDIA RTX 4000 Ada Generation", "rtx4000ada"),
        ("Something New", None),
    ],
)
def test_gpu_names_map_to_catalog_classes(name, cls):
    assert gpu_class_of(name) == cls


def test_every_runpod_gpu_class_exists_in_the_catalog():
    from aivo.catalog.loader import Catalog
    from aivo.config import REPO_ROOT
    from aivo.providers.runpod.catalog import _GPU_CLASSES

    cat = Catalog.load(REPO_ROOT / "catalog")
    assert not {cls for _, cls in _GPU_CLASSES} - set(cat.gpu_classes)


async def test_queue_calls_go_to_the_job_api_and_endpoints_to_the_rest_api():
    seen: list[tuple[str, str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        seen.append((request.method, request.url.host, request.url.path, body))
        assert request.headers["authorization"] == "Bearer rp-key"
        path = request.url.path
        if path.endswith("/run"):
            return httpx.Response(200, json={"id": "job-1", "status": "IN_QUEUE"})
        if "/status/" in path:
            return httpx.Response(200, json={"id": "job-1", "status": "COMPLETED", "executionTime": 1200})
        if path.endswith("/health"):
            return httpx.Response(200, json={"jobs": {"inQueue": 0}, "workers": {"idle": 1, "running": 0}})
        if "/cancel/" in path:
            return httpx.Response(200, json={"id": "job-1", "status": "CANCELLED"})
        if path == "/v2/serverless" and request.method == "POST":
            return httpx.Response(201, json={"id": "ep1", **body})
        if path == "/v2/serverless/ep1" and request.method == "PATCH":
            return httpx.Response(200, json={"id": "ep1", **body})
        return httpx.Response(404, json={"title": "Not Found", "status": 404, "detail": "nope"})

    api = HttpRunPod(
        "rp-key",
        api_base="https://api.runpod.io",
        queue_base="https://api.runpod.ai",
        transport=httpx.MockTransport(handler),
    )
    assert await api.run("ep1", {"job_id": "x"}, {"ttl": 1000}) == "job-1"
    assert (await api.status("ep1", "job-1"))["status"] == "COMPLETED"
    assert (await api.health("ep1"))["workers"]["idle"] == 1
    await api.cancel("ep1", "job-1")
    assert (await api.create_endpoint({"name": "n"}))["id"] == "ep1"
    await api.update_endpoint("ep1", {"workers": {"max": 2}})
    assert await api.get_endpoint("missing") is None
    await api.aclose()
    assert seen[0] == ("POST", "api.runpod.ai", "/v2/ep1/run", {"input": {"job_id": "x"}, "policy": {"ttl": 1000}})
    assert [(m, h, p) for m, h, p, _ in seen[1:4]] == [
        ("GET", "api.runpod.ai", "/v2/ep1/status/job-1"),
        ("GET", "api.runpod.ai", "/v2/ep1/health"),
        ("POST", "api.runpod.ai", "/v2/ep1/cancel/job-1"),
    ]
    assert {h for _, h, p, _ in seen if p.startswith("/v2/serverless")} == {"api.runpod.io"}


class _App:
    """Just enough of AppContext for EndpointManager.desired()."""

    def __init__(self, env: EnvSettings, gpus: GpuCatalog):
        self.env = env
        self.gpus = gpus
        self.deployment_id = uuid.UUID("12345678-1234-5678-1234-567812345678")


async def test_one_queue_endpoint_per_gpu_type_without_secrets():
    env = EnvSettings(_env_file=None, worker_image="ghcr.io/x/aivo-worker:0.2.57", runpod_network_volume_id="vol1")
    gpus = GpuCatalog(FakeRunPod(), env)
    await gpus.offers()
    em = EndpointManager(_App(env, gpus))  # type: ignore[arg-type]
    rs = RuntimeSettings(max_workers_per_gpu=3, keep_warm_s=45, job_timeout_s=1800)
    body = em.desired("NVIDIA L40S", rs, warm=False)
    assert body["name"] == "aivo-12345678-l40s"
    assert body["type"] == "QUEUE" and body["image"] == "ghcr.io/x/aivo-worker:0.2.57"
    assert body["gpu"] == {"pools": ["ADA_48_PRO"], "count": 1, "excludedTypes": ["NVIDIA RTX 6000 Ada Generation"]}
    assert body["workers"] == {"min": 0, "max": 3, "idleTimeout": 45, "initTimeout": 1800}
    assert body["scaling"]["type"] == "QUEUE_DELAY"  # idle timeout isn't allowed with request-count scaling
    assert body["mounts"] == {"global": [], "network": [{"volumeId": "vol1"}]} and body["dataCenterIds"] == ["EU-NL-1"]
    assert set(body["env"]) == {"AIVO_DEPLOYMENT_ID", "AIVO_GPU_TYPE"}  # no tokens or keys
    assert em.desired("NVIDIA L40S", rs, warm=True)["workers"]["min"] == 1
    assert em.config_hash(body) != em.config_hash(em.desired("NVIDIA L40S", rs, warm=True))
    assert gpu_slug("NVIDIA GeForce RTX 4090") == "rtx-4090"


async def test_endpoint_settings_do_not_follow_stock():
    """Stock changes all the time; every endpoint change is a RunPod release that restarts its workers."""
    env = EnvSettings(_env_file=None, worker_image="ghcr.io/x/aivo-worker:0.2.57")
    rp = FakeRunPod()
    rp.stock = {"NVIDIA RTX 6000 Ada Generation": []}  # none in stock in the allowed countries right now
    gpus = GpuCatalog(rp, env)
    await gpus.offers()
    em = EndpointManager(_App(env, gpus))  # type: ignore[arg-type]
    rs = RuntimeSettings()
    body = em.desired("NVIDIA L40S", rs, warm=False)
    # the pool's other type is excluded although the filtered catalog doesn't list it now
    assert body["gpu"]["excludedTypes"] == ["NVIDIA RTX 6000 Ada Generation"]
    assert body["dataCenterIds"] == ["EU-NL-1", "EU-RO-1", "EUR-IS-1"]  # the allowed countries, stock or not
    rp.stock = {"NVIDIA L40S": ["EUR-IS-1"], "NVIDIA H100 80GB HBM3": []}
    await gpus.offers()
    assert em.config_hash(em.desired("NVIDIA L40S", rs, warm=False)) == em.config_hash(body)
    exact = EnvSettings(_env_file=None, worker_image="x", runpod_data_centers=["EU-RO-1", "EU-NL-1"])
    egpus = GpuCatalog(FakeRunPod(), exact)
    await egpus.offers()
    assert egpus.data_centers("NVIDIA L40S") == ["EU-NL-1", "EU-RO-1"]


def test_data_center_countries():
    from aivo.providers.runpod.catalog import dc_country

    cases = {"EU-NL-1": "NL", "EUR-IS-2": "IS", "US-CA-2": "US", "CA-MTL-1": "CA", "AP-JP-1": "JP", "OC-AU-1": "AU"}
    assert {d: dc_country(d) for d in cases} == cases


def test_only_settings_that_differ_from_the_live_endpoint_count():
    from aivo.controller.endpoints import differs

    want = {"pools": ["ADA_48_PRO"], "count": 1, "excludedTypes": ["B", "A"]}
    live = {
        "allowedCudaVersions": [],
        "minCudaVersion": "12.0",
        "pools": ["ADA_48_PRO"],
        "count": 1,
        "excludedTypes": ["A", "B"],
    }
    assert not differs(want, live)  # RunPod's own fields, and the order of names, don't matter
    assert differs({**want, "excludedTypes": ["A"]}, live)
    assert not differs(["EU-RO-1", "EU-NL-1"], ["EU-NL-1", "EU-RO-1"])
    assert differs({"min": 1, "max": 2}, {"min": 0, "max": 2, "idleTimeout": 60})
    assert not differs(3600000, 3600000.0) and differs("FLASHBOOT", "OFF") and differs({"a": 1}, None)
    assert differs({"network": [], "global": [{"volumeId": "gv"}]}, {"network": [], "global": []})


def test_worker_keeps_jobs_in_ram_only_when_it_is_big_enough(tmp_path, monkeypatch):
    import aivo_worker.main as wm

    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(wm.shutil, "disk_usage", lambda p: usage(64 * 1024**2, 0, 64 * 1024**2))
    assert wm.pick_job_dir(str(tmp_path / "shm" / "jobs"), fallback="/fallback") == "/fallback"  # Docker's 64 MB
    monkeypatch.setattr(wm.shutil, "disk_usage", lambda p: usage(32 * 1024**3, 0, 32 * 1024**3))
    assert wm.pick_job_dir(str(tmp_path / "shm" / "jobs"), fallback="/fallback") == str(tmp_path / "shm" / "jobs")


def test_worker_keeps_models_on_the_network_volume(tmp_path, monkeypatch):
    import aivo_worker.main as wm

    monkeypatch.setattr(wm, "NETWORK_VOLUME", tmp_path / "missing")
    assert wm.default_models_dir() == "/var/lib/aivo/models"
    monkeypatch.setattr(wm, "NETWORK_VOLUME", tmp_path)
    assert wm.default_models_dir() == str(tmp_path / "aivo" / "models")


async def test_worker_refuses_input_that_is_not_a_ticket(tmp_path):
    from aivo_worker.engines.mock import MockEngine
    from aivo_worker.runtime import WorkerRuntime

    rt = WorkerRuntime(MockEngine(0.1), models_dir=tmp_path / "m", job_dir=tmp_path / "j", simulate_files=True)
    assert "not an aivo ticket" in (await rt.handle({"input": {"prompt": "hi"}}))["error"]
    old = {"aivo_url": "http://x", "job_id": "j", "token": "t", "protocol": "1.0"}
    assert "protocol" in (await rt.handle({"input": old}))["error"]


async def test_a_global_volume_is_mounted_in_any_data_center_and_the_worker_is_told():
    import pydantic

    env = EnvSettings(_env_file=None, worker_image="ghcr.io/x/aivo-worker:0.2.57", runpod_global_volume_id="gv1")
    gpus = GpuCatalog(FakeRunPod(), env)
    await gpus.offers()
    body = EndpointManager(_App(env, gpus)).desired("NVIDIA L40S", RuntimeSettings(), warm=False)  # type: ignore[arg-type]
    assert body["mounts"] == {"network": [], "global": [{"volumeId": "gv1"}]}
    assert body["env"]["AIVO_VOLUME_KIND"] == "global"
    plain = EnvSettings(_env_file=None, worker_image="ghcr.io/x/aivo-worker:0.2.57")
    pgpus = GpuCatalog(FakeRunPod(), plain)
    await pgpus.offers()
    unpinned = EndpointManager(_App(plain, pgpus)).desired("NVIDIA L40S", RuntimeSettings(), warm=False)  # type: ignore[arg-type]
    assert body["dataCenterIds"] == unpinned["dataCenterIds"]  # wherever the GPU type is, unlike a network volume
    with pytest.raises(pydantic.ValidationError):
        EnvSettings(_env_file=None, runpod_global_volume_id="gv1", runpod_network_volume_id="nv1")


def test_worker_keeps_working_copies_on_its_disk_with_a_global_volume(tmp_path, monkeypatch):
    import aivo_worker.main as wm

    vol = tmp_path / "runpod-volume"
    vol.mkdir()
    monkeypatch.setattr(wm, "NETWORK_VOLUME", vol)
    monkeypatch.delenv("AIVO_VOLUME_KIND", raising=False)
    assert wm.default_models_dir() == str(vol / "aivo" / "models") and wm.shared_models_dir() is None  # network
    monkeypatch.setenv("AIVO_VOLUME_KIND", "global")
    assert wm.default_models_dir() == "/var/lib/aivo/models" and wm.shared_models_dir() == vol / "aivo" / "models"
