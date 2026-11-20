"""Minimal stand-in for ComfyUI's HTTP/WebSocket API, for engine tests."""

import argparse
import asyncio
import json
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int)
ap.add_argument("--output-directory")
ap.add_argument("--listen")
ap.add_argument("--input-directory")
ap.add_argument("--temp-directory")
ap.add_argument("--user-directory")
ap.add_argument("--extra-model-paths-config")
ap.add_argument("--preview-method")
ap.add_argument("--disable-metadata", action="store_true")
ap.add_argument("--disable-auto-launch", action="store_true")
args = ap.parse_args()
assert args.disable_metadata

app = FastAPI()
clients: dict[str, WebSocket] = {}
history: dict[str, dict] = {}
freed = {"n": 0}


@app.get("/system_stats")
async def stats():
    return {"devices": [{"name": "cuda:0 Fake GPU", "vram_total": 48e9, "vram_free": 40e9}]}


@app.post("/free")
async def free(body: dict):
    freed["n"] += 1
    return {}


@app.post("/interrupt")
async def interrupt():
    return {}


@app.get("/history/{pid}")
async def hist(pid: str):
    return {pid: history.get(pid, {})}


@app.post("/history")
async def hist_delete(body: dict):
    for pid in body.get("delete", []):
        history.pop(pid, None)
    return {}


@app.post("/prompt")
async def prompt(body: dict):
    wf = body["prompt"]
    pid = uuid.uuid4().hex
    ws = clients[body["client_id"]]
    asyncio.create_task(run(pid, wf, ws))
    return {"prompt_id": pid, "number": 1, "node_errors": {}}


async def run(pid, wf, ws):
    await asyncio.sleep(0.05)
    for node in wf:
        await ws.send_text(json.dumps({"type": "executing", "data": {"node": node, "prompt_id": pid}}))
        if wf[node]["class_type"] == "KSampler":
            if wf[node]["inputs"].get("steps") == 999:
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "execution_error",
                            "data": {
                                "prompt_id": pid,
                                "exception_type": "torch.OutOfMemoryError",
                                "exception_message": "CUDA out of memory. Tried to allocate 2.00 GiB",
                            },
                        }
                    )
                )
                return
            for i in range(3):
                await ws.send_text(
                    json.dumps({"type": "progress", "data": {"value": i + 1, "max": 3, "prompt_id": pid}})
                )
    outputs = {}
    for node, spec in wf.items():
        if spec["class_type"] == "SaveImage":
            prefix = spec["inputs"]["filename_prefix"]
            out = Path(args.output_directory)
            Image.new("RGB", (64, 64), (200, 10, 10)).save(out / f"{prefix}_00001_.png")
            outputs[node] = {"images": [{"filename": f"{prefix}_00001_.png", "subfolder": "", "type": "output"}]}
    history[pid] = {"outputs": outputs}
    await ws.send_text(json.dumps({"type": "executing", "data": {"node": None, "prompt_id": pid}}))


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, clientId: str):
    await websocket.accept()
    clients[clientId] = websocket
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        clients.pop(clientId, None)


uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
