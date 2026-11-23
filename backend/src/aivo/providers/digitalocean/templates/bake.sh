#!/bin/bash
# aivo: prepare a machine image ("bake"). Runs once on a temporary GPU machine (cloud-init runcmd).
# - installs the container runtime, pre-pulls the worker image and checks that the worker software can
#   use the GPU, then removes this machine's identity and powers off; aivo snapshots the disk
# - boot_status.py reports each step and this log to the backend (pinned TLS + token), so a failure
#   ends the bake right away instead of waiting for a power-off that never comes
# - never prints bootstrap.json (it holds this machine's private key), and deletes it before the snapshot
set -Eeuo pipefail
LOG="${AIVO_SETUP_LOG:-/var/log/aivo-bake.log}"
BOOT="${AIVO_BOOTSTRAP:-/etc/aivo/bootstrap.json}"
STATUS_PY="${AIVO_STATUS_PY:-/opt/aivo/boot_status.py}"
export AIVO_SETUP_LOG="$LOG" AIVO_BOOTSTRAP="$BOOT"
exec >>"$LOG" 2>&1

say() { echo "[aivo $(date -u +%H:%M:%S) UTC] $*"; }
STEP="starting"
stage() { STEP="$1"; say "== step: $1${2:+ ($2)}"; python3 "$STATUS_PY" stage "$1" "${2:-}" || true; }
fail() {
  trap - ERR
  say "FAILED: $1"
  python3 "$STATUS_PY" fail "$1" || true
  exit 1
}
on_err() {
  local code=$? line=$1 cmd=$2
  trap - ERR
  say "FAILED (exit code $code) at line $line: $cmd"
  python3 "$STATUS_PY" fail "Preparing the image failed during '${STEP//_/ }': '$cmd' exited with code $code" || true
  exit "$code"
}
trap 'on_err $LINENO "$BASH_COMMAND"' ERR
retry() {
  local n=0
  until "$@"; do
    n=$((n + 1))
    [ "$n" -ge 3 ] && return 1
    say "retrying in $((n * 15)) s: $*"
    sleep $((n * 15))
  done
}
cfg() { python3 -c "import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print('' if v is None else v)" "$BOOT" "$1"; }

say "bake start $(date -Is)"
mkdir -p /run/aivo
chmod 755 /run/aivo
python3 "$STATUS_PY" stage starting "" || true
setsid nohup python3 "$STATUS_PY" serve >>"$LOG" 2>&1 </dev/null &
IMAGE="$(cfg worker_image)"
say "worker software: $IMAGE"

# 1) GPU driver: an image without it can never run the AI engine
stage checking_gpu
GPU_ARGS=""
if [ -e /dev/kfd ]; then
  GPU_ARGS="--device /dev/kfd --device /dev/dri --group-add video"
  say "AMD GPU found"
elif command -v nvidia-smi >/dev/null 2>&1; then
  if ! GPU_INFO="$(nvidia-smi -L 2>&1)"; then
    echo "$GPU_INFO"
    fail "The NVIDIA GPU driver is installed but not working: $(echo "$GPU_INFO" | tail -n1 | cut -c1-200)"
  fi
  echo "$GPU_INFO"
  GPU_ARGS="--gpus all"
elif grep -qsx 0x10de /sys/bus/pci/devices/*/vendor; then
  fail "This machine has an NVIDIA GPU but no GPU driver: set AIVO_DO_BASE_IMAGE to a GPU image (default gpu-h100x1-base)."
else
  fail "No GPU was found on this machine."
fi

# 2) container runtime
stage installing_runtime
install_docker() { curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sh /tmp/get-docker.sh; }
install_nvidia_toolkit() {
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg &&
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list |
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      >/etc/apt/sources.list.d/nvidia-container-toolkit.list &&
    apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q nvidia-container-toolkit
}
if command -v docker >/dev/null 2>&1; then
  say "Docker is already installed"
else
  retry install_docker || fail "Could not install Docker (the container runtime)."
fi
if [ "$GPU_ARGS" = "--gpus all" ]; then
  if ! command -v nvidia-ctk >/dev/null 2>&1; then
    retry install_nvidia_toolkit || fail "Could not install the NVIDIA container toolkit."
  fi
  grep -qs '"nvidia"' /etc/docker/daemon.json || nvidia-ctk runtime configure --runtime=docker
fi
systemctl enable docker
systemctl restart docker
docker --version

# 3) worker software
stage downloading_software "$IMAGE"
retry docker pull "$IMAGE" || fail "Could not download the worker software ($IMAGE)."

# 4) the worker software must see the GPU (the image's entrypoint is the agent: run python instead)
stage checking_engine
# shellcheck disable=SC2086
if ! OUT="$(docker run --rm $GPU_ARGS --entrypoint python "$IMAGE" -c \
  'import torch; assert torch.cuda.is_available(), "PyTorch cannot see the GPU"; print("GPU for PyTorch:", torch.cuda.get_device_name(0))' 2>&1)"; then
  echo "$OUT"
  fail "The worker software can't use the GPU: $(echo "$OUT" | grep -v '^[[:space:]]*$' | tail -n1 | cut -c1-200)"
fi
echo "$OUT"

# 5) forget this machine's identity, then power off for the snapshot (nothing here may stop the power-off)
stage cleaning_up
say "done: removing this machine's identity and powering off for the snapshot"
python3 "$STATUS_PY" stop || true
trap - ERR
set +e
rm -rf /etc/aivo /opt/aivo /run/aivo
apt-get clean
rm -rf /var/lib/apt/lists/* /var/log/*.gz /tmp/*
cloud-init clean --logs --machine-id
rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys
say "powering off"
sync
poweroff
