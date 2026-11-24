"""RunPod Serverless pieces: worker start-up from the environment, the gateway token header, error mapping."""

from __future__ import annotations

import hashlib
import secrets

import httpx
import pytest

from aivo.providers.base import AuthError, ConfigError, QuotaError, RateLimited, UnknownOutcome, traits_for
from aivo.providers.runpod.client import classify
from aivo.providers.runpod.provider import gpu_class_of
from aivo_protocol.worker_api import BOOTSTRAP_ENV, WorkerBootstrap, decode_bootstrap_env, encode_bootstrap_env
from aivo_worker.engines.mock import MockEngine
from aivo_worker.main import load_bootstrap, pick_job_dir
from aivo_worker.server import WorkerServer, build_state


def _boot(tmp_path, token: str) -> WorkerBootstrap:
    return WorkerBootstrap(
        instance_id="i-1",
        deployment_id="d-1",
        token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        engine="mock",
        models_dir=str(tmp_path / "models"),
        job_dir=str(tmp_path / "jobs"),
    )


def test_bootstrap_travels_in_one_environment_variable(tmp_path):
    boot = _boot(tmp_path, "t")
    value = encode_bootstrap_env(boot)
    assert decode_bootstrap_env(value) == boot
    loaded = load_bootstrap(tmp_path / "missing.json", environ={BOOTSTRAP_ENV: value, "PORT": "8000"})
    assert loaded.listen_port == 8000 and loaded.tls_cert_pem == ""


def test_small_ram_disks_are_not_used_for_jobs(tmp_path, monkeypatch):
    import collections

    import aivo_worker.main as wm

    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(wm.shutil, "disk_usage", lambda p: usage(64 * 1024**2, 0, 64 * 1024**2))
    assert pick_job_dir(str(tmp_path / "shm" / "jobs"), fallback="/fallback") == "/fallback"  # Docker's 64 MB
    monkeypatch.setattr(wm.shutil, "disk_usage", lambda p: usage(32 * 1024**3, 0, 32 * 1024**3))
    assert pick_job_dir(str(tmp_path / "shm" / "jobs"), fallback="/fallback") == str(tmp_path / "shm" / "jobs")


async def test_plain_http_worker_takes_the_token_from_its_own_header(tmp_path):
    token = secrets.token_urlsafe(16)
    state = build_state(_boot(tmp_path, token), MockEngine(0.1), simulate_files=True)
    srv = WorkerServer(state, host="127.0.0.1", port=0)
    await srv.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{srv.port}", trust_env=False) as c:
            assert (await c.get("/ping")).status_code == 200  # the load balancer's check needs no token
            assert (await c.get("/v1/health")).status_code == 401
            # behind RunPod, Authorization carries the RunPod key; X-Aivo-Token wins
            r = await c.get("/v1/health", headers={"Authorization": "Bearer rp-key", "X-Aivo-Token": token})
            assert r.status_code == 200
            assert (await c.get("/v1/health", headers={"X-Aivo-Token": "wrong"})).status_code == 401
            assert (await c.get("/v1/health", headers={"Authorization": f"Bearer {token}"})).status_code == 200
    finally:
        await srv.stop()


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


def test_runpod_bills_from_start_without_minimum():
    t = traits_for("runpod")
    assert t.bills_from_boot and t.min_billing_s == 0 and not t.pinned_tls
    assert traits_for("digitalocean").bills_from_boot is False


def test_every_runpod_gpu_class_exists_in_the_catalog():
    from aivo.catalog.loader import Catalog
    from aivo.config import REPO_ROOT
    from aivo.providers.runpod.provider import _GPU_CLASSES

    cat = Catalog.load(REPO_ROOT / "catalog")
    missing = {cls for _, cls in _GPU_CLASSES} - set(cat.gpu_classes)
    assert not missing
