# aivo worker

Runs on each short-lived GPU machine. A small FastAPI agent (TLS, per-machine
certificate, bearer token) receives fully rendered ComfyUI workflows from the
backend, makes sure the model files are present (cache volume → Spaces mirror →
Hugging Face), runs them in a headless ComfyUI bound to `127.0.0.1`, strips
metadata, and serves the results until the backend has stored them.

* Job inputs/outputs live under `/run/aivo/jobs` (tmpfs): generated content never
  touches a disk on the cloud machine.
* `engines/mock.py` produces placeholders without a GPU (used by the fake provider).
* `Dockerfile` (GPU) and `Dockerfile.mock` (CPU) are built from the repo root.
