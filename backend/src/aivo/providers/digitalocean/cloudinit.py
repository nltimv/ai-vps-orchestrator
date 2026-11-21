"""Render cloud-init user_data for a worker machine.

user_data carries only machine bootstrap data (TLS cert/key, token *hash*,
image reference). Never prompts or any user content.
"""

from __future__ import annotations

import base64
import gzip as gz
from pathlib import Path

import yaml

from aivo_protocol.worker_api import WorkerBootstrap

TEMPLATES = Path(__file__).parent / "templates"
BOOTSTRAP_SH = (TEMPLATES / "bootstrap.sh").read_text()
BOOT_STATUS_PY = (TEMPLATES / "boot_status.py").read_text()
MAX_USER_DATA = 64 * 1024


def _file(path: str, content: str, permissions: str, *, gzip: bool = False) -> dict[str, str]:
    data = content.encode()
    if gzip:  # scripts: keeps user_data well below DigitalOcean's 64 KiB limit
        data = gz.compress(data, mtime=0)
    return {
        "path": path,
        "permissions": permissions,
        "encoding": "gz+b64" if gzip else "b64",
        "content": base64.b64encode(data).decode(),
    }


def render_user_data(boot: WorkerBootstrap) -> str:
    doc = {
        "write_files": [
            _file("/etc/aivo/bootstrap.json", boot.model_dump_json(), "0600"),
            _file("/opt/aivo/bootstrap.sh", BOOTSTRAP_SH, "0755", gzip=True),
            # answers the backend with the current start-up step and log until the agent is up
            _file("/opt/aivo/boot_status.py", BOOT_STATUS_PY, "0755", gzip=True),
        ],
        "runcmd": [["bash", "/opt/aivo/bootstrap.sh"]],
    }
    out = "#cloud-config\n" + yaml.safe_dump(doc, sort_keys=False, width=1_000_000)
    if len(out.encode()) > MAX_USER_DATA:
        raise ValueError(f"user_data too large ({len(out)} bytes)")
    return out
