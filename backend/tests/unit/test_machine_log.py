"""Machine log pieces: the agent's log buffer, the start-up status server, ComfyUI output capture, DO image choice."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import httpx
import pytest
import respx

from aivo.config import EnvSettings
from aivo.providers.base import ConfigError
from aivo.providers.digitalocean.client import classify
from aivo.providers.digitalocean.provider import DigitalOceanProvider, parse_offers
from aivo.providers.fake.provider import BOOT_STATUS_PY, boot_status_module
from aivo.worker_client.client import WorkerClient, WorkerUnreachable
from aivo.worker_client.tls import make_worker_credentials
from aivo_protocol.worker_api import BootStatus, Health, JobIn, OutputSpec, WorkerBootstrap
from aivo_worker.engines.comfy import ComfyEngine
from aivo_worker.engines.mock import MockEngine
from aivo_worker.logbuf import BufferHandler, LogBuffer
from aivo_worker.server import WorkerServer, build_state
from tests.unit.test_comfy_engine import FAKE, free_port, zimage_workflow

# ------------------------------------------------------------------ LogBuffer


def test_log_buffer_numbers_caps_and_reports_dropped_lines():
    buf = LogBuffer(max_lines=5, line_cap=10)
    buf.emit("agent", "one\n\ntwo\r\nthree   ")
    assert [(ln.seq, ln.text) for ln in buf.read().lines] == [(1, "one"), (2, "two"), (3, "three")]
    buf.emit("engine", "x" * 50, "error")
    assert buf.read(3).lines[0].text == "x" * 10 + " …" and buf.read(3).lines[0].level == "error"
    for i in range(5):
        buf.emit("agent", f"line {i}")
    out = buf.read(1)
    assert out.dropped == 3 and out.lines[0].seq == 5 and out.next_seq == 9
    assert buf.read(tail=2).lines[-1].text == "line 4"
    # a cursor from an earlier run of the agent: start over and say so
    again = buf.read(50)
    assert again.reset and again.lines[0].seq == 5
    assert buf.read(9).lines == [] and buf.read(9).next_seq == 9


async def test_log_buffer_long_poll_wakes_on_lines_from_other_threads():
    buf = LogBuffer()
    buf.emit("agent", "first")
    t = asyncio.get_running_loop().time()
    threading.Timer(0.2, lambda: buf.emit("agent", "from a thread")).start()
    out = await buf.wait_read(1, timeout=5)
    assert [ln.text for ln in out.lines] == ["from a thread"]
    assert asyncio.get_running_loop().time() - t < 2
    assert (await buf.wait_read(2, timeout=0.1)).lines == []


def test_buffer_handler_copies_records_without_echo():
    echoed = []
    buf = LogBuffer(echo=echoed.append)
    import logging

    lg = logging.getLogger("aivo_worker.test_handler")
    lg.addHandler(BufferHandler(buf))
    lg.warning("disk %s", "almost full")
    buf.emit("agent", "direct")
    assert [ln.text for ln in buf.read().lines] == ["aivo_worker.test_handler: disk almost full", "direct"]
    assert buf.read().lines[0].level == "warning"
    assert [ln.text for ln in echoed] == ["direct"]  # logging already printed the record itself


# ------------------------------------------------------------------ start-up status server


@pytest.fixture
def machine():
    iid = uuid.uuid4()
    creds = make_worker_credentials(iid)
    token = secrets.token_urlsafe(32)
    boot = {
        "instance_id": str(iid),
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "tls_cert_pem": creds.cert_pem,
        "tls_key_pem": creds.key_pem,
        "listen_port": 443,
    }
    return creds, token, boot


@pytest.fixture
def status_server(tmp_path, machine):
    _creds, _token, boot = machine
    bs = boot_status_module()
    log = tmp_path / "setup.log"
    status = tmp_path / "status.json"
    log.write_text("[aivo] bootstrap start\n\n== step: preparing_disks\n")
    bs.write_status(str(status), stage="preparing_disks")
    srv = bs.make_server(boot, host="127.0.0.1", port=0, status_path=str(status), log_path=str(log))
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    yield srv, log, status
    srv.shutdown()
    srv.server_close()


def client(machine, port: int, *, token: str | None = None, ca: str | None = None) -> WorkerClient:
    creds, tok, _ = machine
    return WorkerClient("127.0.0.1", port, ca or creds.ca_pem, creds.sni_name, token or tok)


async def test_status_server_reports_step_and_serves_the_setup_log(machine, status_server):
    srv, log, status = status_server
    bs = boot_status_module()
    port = srv.server_address[1]
    async with client(machine, port) as c:
        p = await c.probe()
        assert isinstance(p, BootStatus) and p.stage == "preparing_disks" and not p.failed
        assert p.progress_bytes is not None and p.progress_bytes >= 0
        out = await c.logs(0)
        assert [(ln.seq, ln.source, ln.text) for ln in out.lines] == [
            (1, "setup", "[aivo] bootstrap start"),
            (2, "setup", "== step: preparing_disks"),
        ]
        # long-poll: a new line arrives while waiting
        with log.open("a") as fh:
            threading.Timer(0.3, lambda: (fh.write("FAILED: no driver\n"), fh.flush())).start()
            out = await c.logs(2, wait=5)
        assert [ln.text for ln in out.lines] == ["FAILED: no driver"] and out.lines[0].level == "error"
        assert (await c.logs(99)).reset
        bs.write_status(str(status), failed=True, error="No GPU was found on this machine.")
        p = await c.probe()
        assert isinstance(p, BootStatus) and p.failed and p.error == "No GPU was found on this machine."
        with pytest.raises(httpx.HTTPStatusError):  # health() wants the agent, not the status server
            await c.health()

    async with client(machine, port, token="wrong") as c:
        with pytest.raises(WorkerUnreachable, match="credentials"):
            await c.probe()
    other = make_worker_credentials(uuid.uuid4())
    async with client(machine, port, ca=other.ca_pem) as c:
        with pytest.raises(WorkerUnreachable):
            await c.probe()


def test_status_cli_records_steps_and_failures(tmp_path):
    env = {**os.environ, "AIVO_BOOT_STATUS": str(tmp_path / "s.json")}
    run = [sys.executable, "-I", str(BOOT_STATUS_PY)]
    subprocess.run([*run, "stage", "downloading_software", "ghcr.io/x/w:1"], env=env, check=True)
    subprocess.run([*run, "fail", "Could not download the worker software"], env=env, check=True)
    st = json.loads((tmp_path / "s.json").read_text())
    assert st["stage"] == "downloading_software" and st["detail"] == "ghcr.io/x/w:1"
    assert st["failed"] is True and st["error"] == "Could not download the worker software"
    assert subprocess.run([*run, "stop"], env={**env, "AIVO_BOOT_STATUS_PID": str(tmp_path / "none")}).returncode == 0


# ------------------------------------------------------------------ agent


class BrokenEngine(MockEngine):
    async def start(self) -> None:
        raise RuntimeError("CUDA driver initialization failed")


@pytest.fixture
async def agent(tmp_path, machine):
    creds, token, b = machine

    async def make(engine):
        boot = WorkerBootstrap(
            instance_id=b["instance_id"],
            deployment_id="dep",
            token_sha256=b["token_sha256"],
            tls_cert_pem=creds.cert_pem,
            tls_key_pem=creds.key_pem,
            engine="mock",
            models_dir=str(tmp_path / "models"),
            job_dir=str(tmp_path / "jobs"),
        )
        setup = tmp_path / "setup.log"
        setup.write_text("[aivo] bootstrap start\n[aivo] bootstrap done\n")
        state = build_state(boot, engine, simulate_files=True, setup_log=setup)
        srv = WorkerServer(state)
        await srv.start()
        servers.append(srv)
        return srv

    servers: list[WorkerServer] = []
    yield make
    for s in servers:
        await s.stop()


async def test_agent_serves_setup_history_job_lines_and_never_the_prompt(agent, machine):
    srv = await agent(MockEngine(job_seconds=0.2))
    async with client(machine, srv.port) as c:
        for _ in range(50):
            h = await c.probe()
            if isinstance(h, Health) and h.engine_ready:
                break
            await asyncio.sleep(0.05)
        assert isinstance(h, Health) and h.log_seq >= 3
        out = await c.logs(0)
        assert [ln.text for ln in out.lines[:2]] == ["[aivo] bootstrap start", "[aivo] bootstrap done"]
        assert out.lines[0].source == "setup"
        job = JobIn(
            job_id="job-123456789",
            model_id="m",
            workflow={"6": {"inputs": {"text": "secret prompt"}}},
            output=OutputSpec(kind="image", width=64, height=64),
        )
        await c.submit(job)
        for _ in range(100):
            st = await c.job(job.job_id, wait=1)
            if st and st.state.terminal:
                break
        texts = [ln.text for ln in (await c.logs(0)).lines]
        assert any("job job-1234 received: model m, 1x image 64x64" in t for t in texts)
        assert any(t == "got prompt" for t in texts) and any("done in" in t for t in texts)
        assert not any("secret prompt" in t for t in texts)


async def test_agent_reports_an_engine_that_cannot_start(agent, machine):
    srv = await agent(BrokenEngine())
    async with client(machine, srv.port) as c:
        h = await c.health()
        for _ in range(50):
            if h.engine_error:
                break
            await asyncio.sleep(0.05)
            h = await c.health()
        assert not h.engine_ready and h.engine_error == "RuntimeError: CUDA driver initialization failed"
        texts = [ln.text for ln in (await c.logs(0)).lines]
        assert any("AI engine failed to start" in t for t in texts)
        r = await c._req(
            "POST", "/v1/jobs", json=JobIn(job_id="j", model_id="m", output=OutputSpec(kind="image")).model_dump()
        )
        assert r.status_code == 409


# ------------------------------------------------------------------ ComfyUI output capture


async def test_comfy_output_goes_to_the_log_and_stdout(tmp_path, capsys):
    e = ComfyEngine(FAKE, tmp_path / "models", tmp_path / "jobs" / "comfy", port=free_port())
    e.logs = LogBuffer()
    await e.start()
    try:
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        job = JobIn(job_id="j", model_id="z-image-turbo", workflow=zimage_workflow(), output=OutputSpec(kind="image"))
        await e.run(job, tmp_path, out_dir, lambda p, s: None)
        await asyncio.sleep(0.3)
    finally:
        await e.stop()
    lines = e.logs.read().lines
    texts = [ln.text for ln in lines]
    assert all(ln.source == "engine" for ln in lines)
    assert "Starting server" in texts and "got prompt" in texts and "Prompt executed in 0.10 seconds" in texts
    bars = [t for t in texts if "|" in t and "/3" in t]
    assert bars and bars[-1].startswith("100%")  # redraws are thinned out, the final state is kept
    assert len(bars) < 3
    assert "Starting server" in capsys.readouterr().out  # still visible in `docker logs`


async def test_comfy_crash_on_start_says_why(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_COMFY_CRASH", "1")
    e = ComfyEngine(FAKE, tmp_path / "models", tmp_path / "jobs" / "comfy", port=free_port())
    e.logs = LogBuffer()
    with pytest.raises(Exception, match=r"exited with code 3: .*no NVIDIA driver"):
        await e.start()
    await e.stop()
    assert any("Traceback" in ln.text and ln.level == "error" for ln in e.logs.read().lines)


# ------------------------------------------------------------------ DigitalOcean: base image, debug access

SIZES = [
    {
        "slug": "gpu-l40sx1-48gb",
        "memory": 65536,
        "vcpus": 8,
        "disk": 500,
        "price_hourly": 1.57,
        "regions": ["tor1"],
        "available": True,
        "gpu_info": {"count": 1, "model": "nvidia_l40s", "vram": {"amount": 48, "unit": "gib"}},
    }
]


def do_env(**kw) -> EnvSettings:
    return EnvSettings(provider="digitalocean", do_token="t", **kw)


async def test_gpu_machines_use_the_configured_gpu_image_without_fallback():
    offer = parse_offers(SIZES)[0]
    p = DigitalOceanProvider(do_env())
    assert (await p.boot_image(offer, "base")).ref == "gpu-h100x1-base"
    p2 = DigitalOceanProvider(do_env(do_base_image="my-gpu-image"))
    assert (await p2.boot_image(offer, "base")).ref == "my-gpu-image"
    await p.aclose()
    await p2.aclose()


def test_unknown_image_is_a_configuration_error():
    e = classify(
        422,
        {"id": "unprocessable_entity", "message": "You specified an invalid image for Droplet creation."},
        creating=True,
    )
    assert isinstance(e, ConfigError) and "AIVO_DO_BASE_IMAGE" in str(e)


@respx.mock
async def test_debug_ssh_keys_and_port_22_only_when_configured():
    api = "https://api.digitalocean.com"
    respx.get(f"{api}/v2/firewalls").mock(return_value=httpx.Response(200, json={"firewalls": [], "links": {}}))
    created = respx.post(f"{api}/v2/firewalls").mock(return_value=httpx.Response(202, json={"firewall": {"id": "f"}}))
    p = DigitalOceanProvider(do_env(do_ssh_key_ids=["123", "ab:cd"]))
    p._ssh_key_id = 7
    assert await p._ssh_keys() == [7, 123, "ab:cd"]
    await p.ensure_network_access("aivo-dep-1234", ["198.51.100.7/32"], 443)
    rules = json.loads(created.calls[-1].request.content)["inbound_rules"]
    assert [r["ports"] for r in rules] == ["443"]
    await p.ensure_network_access("aivo-dep-1234", ["198.51.100.7/32"], 443, ssh_cidrs=["203.0.113.5/32"])
    rules = json.loads(created.calls[-1].request.content)["inbound_rules"]
    assert rules[1] == {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["203.0.113.5/32"]}}
    await p.aclose()


def test_template_is_valid_for_the_machines_python():
    src = Path(BOOT_STATUS_PY).read_text()
    compile(src, "boot_status.py", "exec")
    assert "bootstrap.json" in src and "print(boot" not in src


def test_retry_after_sold_out_is_not_shown_as_a_new_request():
    from datetime import UTC, datetime

    from aivo.db.models import Event
    from aivo.services.machine_log import render_event

    at = datetime.now(UTC)
    first = Event(scope="instance", type="instance.state", payload={"state": "pending"}, at=at)
    retry = Event(scope="instance", type="instance.state", payload={"state": "pending", "retry": True}, at=at)
    assert render_event(first)["text"] == "Machine requested"
    assert render_event(retry) is None
