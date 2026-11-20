"""Backend <-> worker channel: pinned TLS, SNI, token, job lifecycle against the mock engine."""

import asyncio
import hashlib
import secrets
import uuid

import pytest

from aivo.worker_client.client import WorkerClient, WorkerUnreachable
from aivo.worker_client.tls import make_worker_credentials
from aivo_protocol.worker_api import EnsureFilesIn, FileFetch, JobIn, JobState, OutputSpec, WorkerBootstrap
from aivo_worker.engines.mock import MockEngine
from aivo_worker.server import WorkerServer, build_state


@pytest.fixture
async def worker(tmp_path):
    iid = uuid.uuid4()
    creds = make_worker_credentials(iid)
    token = secrets.token_urlsafe(32)
    boot = WorkerBootstrap(
        instance_id=str(iid), deployment_id="dep", token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        tls_cert_pem=creds.cert_pem, tls_key_pem=creds.key_pem, engine="mock",
        models_dir=str(tmp_path / "models"), job_dir=str(tmp_path / "jobs"),
    )
    state = build_state(boot, MockEngine(job_seconds=0.2), simulate_files=True)
    srv = WorkerServer(state)
    await srv.start()
    yield srv, creds, token, state
    await srv.stop()


async def test_full_job_cycle(worker, tmp_path):
    srv, creds, token, state = worker
    async with WorkerClient("127.0.0.1", srv.port, creds.ca_pem, creds.sni_name, token) as c:
        h = await c.health()
        assert h.engine == "mock" and h.engine_ready
        fs = await c.ensure_files(EnsureFilesIn(files=[FileFetch(dest="x/a.safetensors", bytes=10)]))
        for _ in range(50):
            fs = await c.files_status(["x/a.safetensors"])
            if fs.all_ready:
                break
            await asyncio.sleep(0.05)
        assert fs.all_ready
        await c.put_input("ref.png", b"fake")
        job = JobIn(job_id="j1", model_id="m", inputs=["ref.png"], required_files=["x/a.safetensors"],
                    output=OutputSpec(kind="image", count=2, width=256, height=256))
        st = await c.submit(job)
        assert (await c.submit(job)).job_id == "j1"  # idempotent
        for _ in range(100):
            st = await c.job("j1", wait=2)
            if st.state.terminal:
                break
        assert st.state == JobState.succeeded, st
        assert len(st.outputs) == 2
        dest = tmp_path / "dl.png"
        n = await c.download("j1", st.outputs[0].name, dest)
        assert n == st.outputs[0].bytes
        assert hashlib.sha256(dest.read_bytes()).hexdigest() == st.outputs[0].sha256
        assert dest.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        await c.delete_job("j1")
        # job dir (tmpfs in production) is gone after ack
        assert not (state.executor.job_root / "j1").exists()
        assert await c.job("j1") is None
        new = await c.rotate_token()
        assert new != token
        assert (await c.health()).engine_ready


async def test_wrong_ca_is_rejected(worker):
    srv, creds, token, _ = worker
    other = make_worker_credentials(uuid.uuid4())
    async with WorkerClient("127.0.0.1", srv.port, other.ca_pem, creds.sni_name, token) as c:
        with pytest.raises(Exception):
            await c.health()


async def test_wrong_sni_is_rejected(worker):
    srv, creds, token, _ = worker
    async with WorkerClient("127.0.0.1", srv.port, creds.ca_pem, "i-other.worker.aivo.invalid", token) as c:
        with pytest.raises(Exception):
            await c.health()


async def test_bad_token_rejected(worker):
    srv, creds, _, _ = worker
    async with WorkerClient("127.0.0.1", srv.port, creds.ca_pem, creds.sni_name, "nope") as c:
        with pytest.raises(WorkerUnreachable):
            await c.health()


async def test_video_and_audio_outputs(worker, tmp_path):
    srv, creds, token, _ = worker
    async with WorkerClient("127.0.0.1", srv.port, creds.ca_pem, creds.sni_name, token) as c:
        await c.submit(JobIn(job_id="v", model_id="m", output=OutputSpec(kind="video", duration_s=1, fps=8,
                                                                          width=320, height=240, with_audio=True)))
        await c.submit(JobIn(job_id="a", model_id="m", output=OutputSpec(kind="audio", duration_s=2)))
        for jid, mt in (("v", "video/mp4"), ("a", "audio/mp4")):
            for _ in range(100):
                st = await c.job(jid, wait=2)
                if st.state.terminal:
                    break
            assert st.state == JobState.succeeded, st
            assert st.outputs[0].media_type == mt
            assert st.outputs[0].duration_s and st.outputs[0].duration_s > 0.5


async def test_drain_rejects_new_jobs(worker):
    srv, creds, token, _ = worker
    async with WorkerClient("127.0.0.1", srv.port, creds.ca_pem, creds.sni_name, token) as c:
        h = await c.drain()
        assert h.draining
        with pytest.raises(WorkerUnreachable):
            await c.submit(JobIn(job_id="z", model_id="m", output=OutputSpec(kind="image")))
