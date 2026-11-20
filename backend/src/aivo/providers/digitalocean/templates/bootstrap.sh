#!/bin/bash
# aivo worker bootstrap. Runs once per machine (cloud-init runcmd). Idempotent.
# - job inputs/outputs live on tmpfs only: generated content never touches a disk here
# - model weights go to the local scratch NVMe when present, else the boot disk
# - the shared cache volume (public model files only) is mounted read-only
# - containers cannot reach the cloud metadata service
set -euo pipefail
exec >>/var/log/aivo-bootstrap.log 2>&1
echo "[aivo] bootstrap start $(date -Is)"

BOOT=/etc/aivo/bootstrap.json
cfg() { python3 -c "import json,sys; v=json.load(open('$BOOT')).get('$1'); print('' if v is None else v)"; }
IMAGE="$(cfg worker_image)"
PORT="$(cfg listen_port)"
VOLUME_MOUNT="$(cfg volume_mount)"

# 1) RAM-backed job directory
mkdir -p /run/aivo/jobs
mountpoint -q /run/aivo/jobs || mount -t tmpfs -o size=40%,mode=0700 tmpfs /run/aivo/jobs

# 2) model storage on scratch NVMe if available (H100/H200 sizes have one)
mkdir -p /var/lib/aivo/models
if ! mountpoint -q /var/lib/aivo/models; then
  VOLREAL=""
  for v in /dev/disk/by-id/scsi-0DO_Volume_*; do [ -e "$v" ] && VOLREAL="$(readlink -f "$v")"; done
  for dev in /dev/nvme0n1 /dev/nvme1n1 /dev/vdb /dev/vdc; do
    [ -b "$dev" ] || continue
    [ "$dev" = "$VOLREAL" ] && continue
    lsblk -no MOUNTPOINT "$dev" | grep -q . && continue
    [ "$(lsblk -no TYPE "$dev" | head -n1)" = "disk" ] || continue
    [ -n "$(lsblk -no NAME "$dev" | tail -n +2)" ] && continue   # partitioned -> not scratch
    blkid "$dev" >/dev/null 2>&1 || mkfs.ext4 -F -q -E nodiscard "$dev"
    mount -o noatime "$dev" /var/lib/aivo/models && echo "[aivo] using scratch disk $dev" && break
  done
fi

# 3) read-only cache volume
if [ -n "$VOLUME_MOUNT" ]; then
  mkdir -p "$VOLUME_MOUNT"
  for v in /dev/disk/by-id/scsi-0DO_Volume_*; do
    [ -e "$v" ] || continue
    mountpoint -q "$VOLUME_MOUNT" || mount -o ro,noatime "$v" "$VOLUME_MOUNT" || true
  done
fi

# 4) container runtime (preinstalled on the prepared machine image)
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
if ! command -v nvidia-ctk >/dev/null 2>&1 && command -v nvidia-smi >/dev/null 2>&1; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
fi
systemctl enable --now docker

# 5) containers must not read user_data/metadata
iptables -C DOCKER-USER -d 169.254.169.254/32 -j DROP 2>/dev/null || iptables -I DOCKER-USER -d 169.254.169.254/32 -j DROP

# 6) worker image (only the changed layers are pulled when the prepared image is a bit stale)
docker pull -q "$IMAGE" || docker image inspect "$IMAGE" >/dev/null

# 7) run the agent
docker rm -f aivo-worker >/dev/null 2>&1 || true
GPU_ARGS="--gpus all"
if [ -e /dev/kfd ]; then GPU_ARGS="--device /dev/kfd --device /dev/dri --group-add video"; fi
VOL_ARGS=""
if [ -n "$VOLUME_MOUNT" ] && mountpoint -q "$VOLUME_MOUNT"; then VOL_ARGS="-v $VOLUME_MOUNT:$VOLUME_MOUNT:ro"; fi
# shellcheck disable=SC2086
docker run -d --name aivo-worker --restart unless-stopped $GPU_ARGS --shm-size 16g \
  -p "$PORT:$PORT" \
  -v /etc/aivo:/etc/aivo:ro \
  -v /var/lib/aivo/models:/var/lib/aivo/models \
  -v /run/aivo/jobs:/run/aivo/jobs \
  $VOL_ARGS \
  -e AIVO_WORKER_BOOTSTRAP=/etc/aivo/bootstrap.json \
  "$IMAGE"
echo "[aivo] bootstrap done $(date -Is)"
