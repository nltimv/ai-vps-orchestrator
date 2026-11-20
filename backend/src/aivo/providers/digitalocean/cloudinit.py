"""Render cloud-init user_data for a worker machine.

user_data carries only machine bootstrap data (TLS cert/key, token *hash*,
image reference). Never prompts or any user content.
"""

from __future__ import annotations

import base64
from pathlib import Path

import yaml

from aivo_protocol.worker_api import WorkerBootstrap

BOOTSTRAP_SH = (Path(__file__).parent / "templates" / "bootstrap.sh").read_text()
MAX_USER_DATA = 64 * 1024


def render_user_data(boot: WorkerBootstrap) -> str:
    doc = {
        "write_files": [
            {
                "path": "/etc/aivo/bootstrap.json",
                "permissions": "0600",
                "encoding": "b64",
                "content": base64.b64encode(boot.model_dump_json().encode()).decode(),
            },
            {
                "path": "/opt/aivo/bootstrap.sh",
                "permissions": "0755",
                "encoding": "b64",
                "content": base64.b64encode(BOOTSTRAP_SH.encode()).decode(),
            },
        ],
        "runcmd": [["bash", "/opt/aivo/bootstrap.sh"]],
    }
    out = "#cloud-config\n" + yaml.safe_dump(doc, sort_keys=False, width=1_000_000)
    if len(out.encode()) > MAX_USER_DATA:
        raise ValueError(f"user_data too large ({len(out)} bytes)")
    return out
