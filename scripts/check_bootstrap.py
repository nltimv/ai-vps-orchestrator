"""Run the machine start-up script (bootstrap.sh + boot_status.py) in an Ubuntu 24.04 container.

Checks what a real GPU machine would show in the dashboard, without a GPU or a cloud account:
``docker``, ``nvidia-smi`` and friends are stubbed; everything else is the real script.

    uv run python scripts/check_bootstrap.py

Needs a local Docker daemon. Builds ``aivo-bootstrap-check`` (ubuntu:24.04 + python3) on first use.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import textwrap
import uuid
from pathlib import Path

from aivo.worker_client.tls import make_worker_credentials

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "backend/src/aivo/providers/digitalocean/templates"
IMAGE = "aivo-bootstrap-check"

DOCKER_STUB = r"""#!/bin/bash
echo "docker $*" >> /tmp/docker-calls
case "$1" in
  --version) echo "Docker version 27.0.0 (stub)" ;;
  pull) echo "latest: Pulling from aivo-worker"; echo "Status: Downloaded newer image" ;;
  image|rm|update) exit 0 ;;
  run)
    if [[ " $* " == *" --rm "* ]]; then
      if [ "$STUB_MODE" = gpu_broken ]; then
        echo 'docker: Error response from daemon: could not select device driver "" with capabilities: [[gpu]].' >&2
        exit 125
      fi
      echo "GPU 0: NVIDIA L40S (UUID: GPU-stub)"; exit 0
    fi
    echo c0ffee ;;
  logs)
    if [ "$STUB_MODE" = crash ]; then echo "Traceback (most recent call last):"; echo "Error: boom"
    else echo "AI engine ready after 3 s"; fi ;;
  inspect) if [ "$STUB_MODE" = crash ]; then echo "restarting 3"; else echo "running 0"; fi ;;
esac
"""
NVIDIA_SMI_STUB = """#!/bin/bash
case "$1" in
  -L) echo "GPU 0: NVIDIA L40S (UUID: GPU-stub)" ;;
  *) echo "580.95.05, 46068 MiB" ;;
esac
"""
NOOP = "#!/bin/bash\nexit 0\n"
IPTABLES_STUB = '#!/bin/bash\n[ "$1" = "-C" ] && exit 1\nexit 0\n'
RECORD = '#!/bin/bash\necho "$(basename "$0") $*" >> /tmp/disk-calls\n'

# Disk tools answer from /check/disks.json, so the check never looks at (or formats) a real disk.
LSBLK_STUB = """#!/usr/bin/env python3
import json, sys
disks, a = json.load(open("/check/disks.json")), sys.argv[1:]
if "-bdpno" in a:
    for name, d in disks.items():
        print(name, d["type"], d["ro"], d["size"])
elif "-no" in a:
    col, dev = a[a.index("-no") + 1], a[-1]
    d = disks.get(dev)
    if d is None:
        sys.exit(32)
    if col == "NAME":
        print("\\n".join([dev.rsplit("/", 1)[1], *d.get("parts", [])]))
    elif col == "MOUNTPOINT":
        print(d.get("mount", ""))
"""
BLKID_STUB = """#!/usr/bin/env python3
import json, sys
disks, a = json.load(open("/check/disks.json")), sys.argv[1:]
d = disks.get(a[-1], {})
v = d.get({"TYPE": "fstype", "LABEL": "label"}.get(a[a.index("-s") + 1] if "-s" in a else "TYPE", "fstype"))
if not v:
    sys.exit(2)
print(v)
"""
GB = 1024**3
# A DigitalOcean RTX 4000/6000 Ada or L40S droplet: partitioned boot disk + the read-only config drive.
DO_DISKS = {
    "/dev/vda": {"type": "disk", "ro": 0, "size": 500 * GB, "parts": ["vda1", "vda14", "vda15"], "mount": "/"},
    "/dev/vdb": {"type": "disk", "ro": 1, "size": 485376, "fstype": "iso9660", "label": "config-2"},
}
# An H100 droplet also has a blank local scratch disk.
H100_DISKS = DO_DISKS | {"/dev/nvme0n1": {"type": "disk", "ro": 0, "size": 5000 * GB}}

PROBE = r"""
import json, socket, ssl, sys
token, sni = sys.argv[1], sys.argv[2]
ctx = ssl.create_default_context(cafile="/check/ca.pem")  # the machine's own CA, as the backend pins it
def get(path):
    with ctx.wrap_socket(socket.create_connection(("127.0.0.1", 443), 10), server_hostname=sni) as s:
        req = f"GET {path} HTTP/1.1\r\nHost: m\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n"
        s.sendall(req.encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)
try:
    out = {"health": get("/v1/health"), "logs": get("/v1/logs?since=0")}
except OSError as e:
    out = {"error": type(e).__name__ + ": " + str(e)}
print("PROBE " + json.dumps(out))
"""


def ensure_image() -> None:
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0:
        return
    print("building", IMAGE)
    dockerfile = (
        "FROM ubuntu:24.04\nRUN apt-get update -q && apt-get install -y -q python3 && rm -rf /var/lib/apt/lists/*\n"
    )
    subprocess.run(["docker", "build", "-q", "-t", IMAGE, "-"], input=dockerfile.encode(), check=True)


def run_case(
    name: str,
    *,
    engine: str,
    mode: str,
    nvidia: bool,
    pci_vendor: str | None,
    wait_after_s: int = 2,
    disks: dict | None = None,
    read_only_models: bool = False,
) -> dict:
    iid = uuid.uuid4()
    creds = make_worker_credentials(iid)
    token = secrets.token_urlsafe(24)
    work = Path(tempfile.mkdtemp(prefix=f"aivo-check-{name}-"))
    try:
        (work / "etc").mkdir()
        boot = {
            "instance_id": str(iid),
            "deployment_id": "check",
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "tls_cert_pem": creds.cert_pem,
            "tls_key_pem": creds.key_pem,
            "listen_host": "0.0.0.0",
            "listen_port": 443,
            "engine": engine,
            "worker_image": "ghcr.io/example/aivo-worker:latest",
        }
        (work / "etc/bootstrap.json").write_text(json.dumps(boot))
        (work / "ca.pem").write_text(creds.ca_pem)
        (work / "probe.py").write_text(PROBE)
        (work / "disks.json").write_text(json.dumps(disks or DO_DISKS))
        stubs = work / "stubs"
        stubs.mkdir()
        files = {
            "docker": DOCKER_STUB,
            "systemctl": NOOP,
            "iptables": IPTABLES_STUB,
            "lsblk": LSBLK_STUB,
            "blkid": BLKID_STUB,
            "mount": RECORD,
            "umount": RECORD,
            "mkfs.ext4": RECORD,
        }
        if nvidia:
            files |= {"nvidia-smi": NVIDIA_SMI_STUB, "nvidia-ctk": NOOP}
        for f, body in files.items():
            (stubs / f).write_text(body)
            (stubs / f).chmod(0o755)
        args = [
            "docker", "run", "--rm", "--network", "none",
            "-e", f"STUB_MODE={mode}",
            "-e", "PATH=/check/stubs:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "-v", f"{work}:/check",
            "-v", f"{work}/etc:/etc/aivo:ro",
            "-v", f"{TEMPLATES}/bootstrap.sh:/opt/aivo/bootstrap.sh:ro",
            "-v", f"{TEMPLATES}/boot_status.py:/opt/aivo/boot_status.py:ro",
        ]  # fmt: skip
        if read_only_models:
            (work / "models").mkdir()
            args += ["-v", f"{work}/models:/var/lib/aivo/models:ro"]
        if pci_vendor:
            dev = work / "pci/0000:00:05.0"
            dev.mkdir(parents=True)
            (dev / "vendor").write_text(pci_vendor + "\n")
            args += ["-v", f"{work}/pci:/sys/bus/pci/devices:ro"]
        script = textwrap.dedent(f"""
            bash /opt/aivo/bootstrap.sh; echo "EXIT $?"
            sleep {wait_after_s}
            python3 /check/probe.py {token} {creds.sni_name}
            echo "DOCKER-CALLS"; cat /tmp/docker-calls 2>/dev/null
            echo "DISK-CALLS"; cat /tmp/disk-calls 2>/dev/null
            echo "SETUP-LOG"; cat /var/log/aivo-bootstrap.log
        """)
        out = subprocess.run([*args, IMAGE, "bash", "-c", script], capture_output=True, text=True, timeout=300)
        text = out.stdout
        exit_code = int(text.split("EXIT ", 1)[1].split()[0])
        probe = json.loads(text.split("PROBE ", 1)[1].split("\n", 1)[0])
        calls = text.split("DOCKER-CALLS\n", 1)[1].split("DISK-CALLS\n", 1)[0]
        disk_calls = text.split("DISK-CALLS\n", 1)[1].split("SETUP-LOG\n", 1)[0]
        log = text.split("SETUP-LOG\n", 1)[1]
        return {"exit": exit_code, "probe": probe, "calls": calls + disk_calls, "log": log, "stderr": out.stderr}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check(cond: bool, what: str, ctx: dict) -> None:
    if not cond:
        print(
            f"FAIL: {what}\n--- setup log ---\n{ctx['log']}\n"
            f"--- probe ---\n{ctx['probe']}\n--- calls ---\n{ctx['calls']}"
        )
        sys.exit(1)
    print(f"  ok: {what}")


def main() -> None:
    ensure_image()

    print("1) NVIDIA GPU but no driver (plain Ubuntu image)")
    r = run_case("nodriver", engine="comfy", mode="ok", nvidia=False, pci_vendor="0x10de")
    check(r["exit"] == 1, "script stops with an error", r)
    status, body = r["probe"]["health"]
    st = body["bootstrap"]
    check(status == 503 and st["failed"] and st["stage"] == "checking_gpu", "status server reports the failure", r)
    check("no GPU driver" in st["error"] and "AIVO_DO_BASE_IMAGE" in st["error"], "the reason names the fix", r)
    lines = [ln["text"] for ln in r["probe"]["logs"][1]["lines"]]
    check(
        any("FAILED" in t for t in lines) and lines == [x for x in r["log"].splitlines() if x.strip()], "log served", r
    )
    check("token_sha256" not in r["log"] and "PRIVATE KEY" not in r["log"], "no secrets in the log", r)
    check("pull" not in r["calls"], "fails before downloading anything", r)

    print("2) driver present, but containers can't use the GPU")
    r = run_case("gpubroken", engine="comfy", mode="gpu_broken", nvidia=True, pci_vendor="0x10de")
    st = r["probe"]["health"][1]["bootstrap"]
    check(r["exit"] == 1 and st["failed"] and "can't be used inside containers" in st["error"], "reported", r)
    check("could not select device driver" in st["error"], "with docker's own message", r)

    print("3) healthy start on an RTX/L40S size: hands over to the agent")
    r = run_case("ok", engine="comfy", mode="ok", nvidia=True, pci_vendor="0x10de")
    check(r["exit"] == 0, "script succeeds", r)
    check("/dev/vdb" not in r["calls"] and "mkfs" not in r["calls"], "the read-only config drive is left alone", r)
    check("model files go on the boot disk" in r["log"] and " GB free" in r["log"], "models on the boot disk", r)
    check(
        "error" in r["probe"] and "Refused" in r["probe"]["error"], "status server stopped (port free for the agent)", r
    )
    run = next(ln for ln in r["calls"].splitlines() if ln.startswith("docker run -d"))
    check("--gpus all" in run and "-p 443:443" in run, "agent started with the GPU on the worker port", r)
    check("/var/log/aivo-bootstrap.log:/var/log/aivo-bootstrap.log:ro" in run, "setup log handed to the agent", r)
    check(r["log"].count("== step:") == 5 and "bootstrap done" in r["log"], "all steps logged", r)

    print("4) agent keeps crashing: the watcher reports it (takes ~25 s)")
    r = run_case("crash", engine="mock", mode="crash", nvidia=False, pci_vendor=None, wait_after_s=27)
    st = r["probe"]["health"][1]["bootstrap"]
    check(st["failed"] and "keeps stopping: Error: boom" in st["error"], "failure reported by the restarted server", r)
    check("  | Error: boom" in r["log"], "the agent's last output is in the setup log", r)

    print("5) H100 size: blank scratch disk is formatted and used for models")
    r = run_case("h100", engine="comfy", mode="ok", nvidia=True, pci_vendor="0x10de", disks=H100_DISKS)
    check(r["exit"] == 0, "script succeeds", r)
    check("mkfs.ext4 -F -q -L aivo-scratch -E nodiscard /dev/nvme0n1" in r["calls"], "scratch disk formatted", r)
    check("mount -o noatime /dev/nvme0n1 /var/lib/aivo/models" in r["calls"], "and mounted for models", r)
    check("/dev/vdb" not in r["calls"], "config drive still left alone", r)

    print("6) model storage is read-only")
    r = run_case("ro", engine="comfy", mode="ok", nvidia=True, pci_vendor="0x10de", read_only_models=True)
    st = r["probe"]["health"][1]["bootstrap"]
    check(r["exit"] == 1 and st["failed"] and "read-only" in st["error"], "reported before downloading anything", r)
    check("pull" not in r["calls"], "nothing downloaded", r)
    print("bootstrap check: OK")


if __name__ == "__main__":
    main()
