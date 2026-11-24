# syntax=docker/dockerfile:1.7
# GPU worker image for RunPod Serverless: CUDA + PyTorch + ComfyUI (pinned) + the aivo worker.
# Layers are ordered from least to most frequently changing, so RunPod hosts that already have an
# older release only pull the small top layers.
# Build from the repository root:  docker build -f worker/Dockerfile -t aivo-worker .
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-dev git ffmpeg libgl1 libglib2.0-0 ca-certificates curl build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# 1) PyTorch (largest, rarely changes)
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
ARG TORCH_VERSION=
RUN pip install "torch${TORCH_VERSION:+==$TORCH_VERSION}" torchvision torchaudio --index-url "$TORCH_INDEX_URL"

# 2) ComfyUI pinned to a release that natively supports the catalog's models
ARG COMFYUI_REPO=https://github.com/comfyanonymous/ComfyUI.git
ARG COMFYUI_REF=v0.32.0
RUN git clone --filter=blob:none "$COMFYUI_REPO" /opt/ComfyUI \
    && git -C /opt/ComfyUI checkout "$COMFYUI_REF" \
    && pip install -r /opt/ComfyUI/requirements.txt \
    && rm -rf /opt/ComfyUI/.git /opt/ComfyUI/models

# 3) RunPod's worker SDK (pulls jobs from the endpoint's queue)
ARG RUNPOD_SDK_VERSION=2.2.1
RUN pip install "runpod==${RUNPOD_SDK_VERSION}"

# 4) aivo worker (changes most often)
COPY packages/protocol /src/protocol
COPY worker /src/worker
RUN pip install /src/protocol /src/worker

ENV AIVO_COMFY_DIR=/opt/ComfyUI
# Release version, set by the release workflow (last, so a new version doesn't invalidate the big layers)
ARG AIVO_VERSION=dev
ENV AIVO_VERSION=$AIVO_VERSION
# Runs as a RunPod Serverless worker: takes jobs from the queue, runs ComfyUI on 127.0.0.1 only and
# delivers results straight to aivo. Nothing listens on a public port.
ENTRYPOINT ["aivo-worker"]
