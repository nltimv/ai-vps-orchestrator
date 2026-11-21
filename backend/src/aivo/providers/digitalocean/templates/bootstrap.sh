#!/bin/bash
# aivo worker bootstrap. Runs once per machine (cloud-init runcmd). Idempotent.
# - job inputs/outputs live on tmpfs only: generated content never touches a disk here
# - model weights go to the local scratch NVMe when present, else the boot disk
# - the shared cache volume (public model files only) is mounted read-only
# - containers cannot reach the cloud metadata service
# - until the worker agent is up, boot_status.py answers the backend with the current step and this
#   log, so problems are visible in the dashboard ("Show log") instead of a silent timeout
# - never prints bootstrap.json (it holds the machine's private key)
set -Eeuo pipefail
LOG="${AIVO_SETUP_LOG:-/var/log/aivo-bootstrap.log}"
BOOT="${AIVO_BOOTSTRAP:-/etc/aivo/bootstrap.json}"
STATUS_PY="${AIVO_STATUS_PY:-/opt/aivo/boot_status.py}"
SELF="$(readlink -f "$0")"
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
  python3 "$STATUS_PY" fail "Start-up failed during '${STEP//_/ }': '$cmd' exited with code $code" || true
  exit "$code"
}
trap 'on_err $LINENO "$BASH_COMMAND"' ERR

# Run a command in the background so it outlives this script (systemd unit when available).
bg_run() {
  local unit=$1; shift
  if [ -d /run/systemd/system ] && command -v systemd-run >/dev/null 2>&1; then
    systemctl stop "$unit" >/dev/null 2>&1 || true
    systemctl reset-failed "$unit" >/dev/null 2>&1 || true
    systemd-run --unit="$unit" --collect --quiet \
      --setenv=AIVO_SETUP_LOG="$LOG" --setenv=AIVO_BOOTSTRAP="$BOOT" --setenv=AIVO_STATUS_PY="$STATUS_PY" "$@"
  else
    setsid nohup "$@" >>"$LOG" 2>&1 </dev/null &
  fi
}
start_status_server() { python3 "$STATUS_PY" stop || true; bg_run aivo-boot-status python3 "$STATUS_PY" serve; }
retry() {
  local n=0
  until "$@"; do
    n=$((n + 1))
    [ "$n" -ge 3 ] && return 1
    say "retrying in $((n * 15)) s: $*"
    sleep $((n * 15))
  done
}
has_pci_vendor() { grep -qsx "$1" /sys/bus/pci/devices/*/vendor; }
cfg() { python3 -c "import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print('' if v is None else v)" "$BOOT" "$1"; }

# Watch the worker container for its first 15 minutes: if it keeps stopping before the AI engine
# is ready, keep its last output, stop it and report the failure through the status server.
watch_container() {
  set +eE +o pipefail
  trap - ERR
  local deadline=$(($(date +%s) + 900)) state restarts last
  sleep 20
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if docker logs aivo-worker 2>&1 | grep -c "AI engine ready" >/dev/null; then return 0; fi
    state="$(docker inspect -f '{{.State.Status}} {{.RestartCount}}' aivo-worker 2>/dev/null || echo 'missing 0')"
    restarts="${state##* }"
    state="${state%% *}"
    if [ "${restarts:-0}" -ge 2 ] || [ "$state" = exited ] || [ "$state" = dead ] || [ "$state" = missing ]; then
      say "the worker software stopped ($state, restarted ${restarts:-0} times). Its last output:"
      docker logs --tail 80 aivo-worker 2>&1 | sed 's/^/  | /'
      last="$(docker logs --tail 20 aivo-worker 2>&1 | grep -v '^[[:space:]]*$' | tail -n 1 | cut -c1-300)"
      docker rm -f aivo-worker >/dev/null 2>&1
      python3 "$STATUS_PY" fail "The worker software keeps stopping${last:+: $last}"
      start_status_server
      return 0
    fi
    sleep 10
  done
}
if [ "${1:-}" = "watch" ]; then
  watch_container
  exit 0
fi

say "bootstrap start $(date -Is)"
mkdir -p /run/aivo
chmod 755 /run/aivo
python3 "$STATUS_PY" stage starting "" || true
start_status_server

IMAGE="$(cfg worker_image)"
PORT="$(cfg listen_port)"
ENGINE="$(cfg engine)"
VOLUME_MOUNT="$(cfg volume_mount)"
say "worker software: $IMAGE, engine: $ENGINE, port: $PORT"

# 1) disks -------------------------------------------------------------------------------
stage preparing_disks
# RAM-backed job directory
mkdir -p /run/aivo/jobs
mountpoint -q /run/aivo/jobs || mount -t tmpfs -o size=40%,mode=0700 tmpfs /run/aivo/jobs

# Model storage: a local scratch disk if the size has one (H100/H200), else the boot disk.
# Only a writable, large, unpartitioned disk that is blank (or that we formatted before) qualifies.
# Never DigitalOcean's read-only config drive (/dev/vdb, under 1 MB) or an attached volume.
is_volume() {
  local v
  for v in /dev/disk/by-id/scsi-0DO_Volume_*; do
    [ -e "$v" ] && [ "$(readlink -f "$v")" = "$1" ] && return 0
  done
  return 1
}
pick_scratch_disk() {
  local name type ro size fstype label
  while read -r name type ro size; do
    [ "$type" = disk ] && [ "$ro" = 0 ] || continue
    [ "${size:-0}" -ge $((100 * 1024 * 1024 * 1024)) ] || continue
    is_volume "$name" && continue
    [ "$(lsblk -no NAME "$name" | wc -l)" -eq 1 ] || continue # partitioned: the boot disk
    [ -z "$(lsblk -no MOUNTPOINT "$name" | tr -d '[:space:]')" ] || continue
    fstype="$(blkid -o value -s TYPE "$name" 2>/dev/null || true)"
    label="$(blkid -o value -s LABEL "$name" 2>/dev/null || true)"
    [ -z "$fstype" ] || [ "$label" = aivo-scratch ] || continue
    echo "$name"
    return 0
  done < <(lsblk -bdpno NAME,TYPE,RO,SIZE)
  return 1
}
writable() { touch "$1/.aivo-write-test" 2>/dev/null && rm -f "$1/.aivo-write-test"; }

mkdir -p /var/lib/aivo/models
if ! mountpoint -q /var/lib/aivo/models; then
  if SCRATCH="$(pick_scratch_disk)"; then
    [ -n "$(blkid -o value -s TYPE "$SCRATCH" 2>/dev/null || true)" ] || mkfs.ext4 -F -q -L aivo-scratch -E nodiscard "$SCRATCH"
    if mount -o noatime "$SCRATCH" /var/lib/aivo/models; then
      if writable /var/lib/aivo/models; then
        say "using scratch disk $SCRATCH"
      else
        say "warning: scratch disk $SCRATCH is not writable, using the boot disk instead"
        umount /var/lib/aivo/models
      fi
    fi
  else
    say "no scratch disk on this machine: model files go on the boot disk"
  fi
fi
writable /var/lib/aivo/models || fail "The model storage on this machine (/var/lib/aivo/models) is read-only."
FREE_GB="$(df -BG --output=avail /var/lib/aivo/models | tail -n1 | tr -dc '0-9')"
say "model storage: ${FREE_GB:-?} GB free"
[ "${FREE_GB:-0}" -ge 30 ] || say "warning: little space for model files (${FREE_GB:-0} GB)"

# read-only cache volume
if [ -n "$VOLUME_MOUNT" ]; then
  mkdir -p "$VOLUME_MOUNT"
  for v in /dev/disk/by-id/scsi-0DO_Volume_*; do
    [ -e "$v" ] || continue
    mountpoint -q "$VOLUME_MOUNT" || mount -o ro,noatime "$v" "$VOLUME_MOUNT" || say "warning: could not mount the model disk"
  done
fi

# 2) GPU driver (checked first: a missing driver is the most common cause of a machine never starting)
stage checking_gpu
GPU=none
GPU_ARGS=""
if [ "$ENGINE" = "mock" ]; then
  say "test engine: no GPU needed"
elif [ -e /dev/kfd ]; then
  GPU=amd
  GPU_ARGS="--device /dev/kfd --device /dev/dri --group-add video"
  say "AMD GPU found"
elif command -v nvidia-smi >/dev/null 2>&1; then
  if ! GPU_INFO="$(nvidia-smi -L 2>&1)"; then
    echo "$GPU_INFO"
    fail "The NVIDIA GPU driver is installed but not working: $(echo "$GPU_INFO" | tail -n1 | cut -c1-200)"
  fi
  echo "$GPU_INFO"
  nvidia-smi --query-gpu=driver_version,memory.total --format=csv,noheader || true
  GPU=nvidia
  GPU_ARGS="--gpus all"
elif has_pci_vendor 0x10de; then
  fail "This machine has an NVIDIA GPU but no GPU driver. It was started from an operating-system image without GPU drivers: set AIVO_DO_BASE_IMAGE to a GPU image (default gpu-h100x1-base)."
elif has_pci_vendor 0x1002; then
  fail "This machine has an AMD GPU but no GPU driver. It was started from an operating-system image without GPU drivers: set AIVO_DO_BASE_IMAGE_AMD to a GPU image (default gpu-amd-base)."
else
  fail "No GPU was found on this machine."
fi

# 3) container runtime (preinstalled on the prepared machine image) ------------------------
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
  retry install_docker || fail "Could not install Docker (the container runtime). Check the machine's internet access."
fi
if [ "$GPU" = nvidia ]; then
  if ! command -v nvidia-ctk >/dev/null 2>&1; then
    retry install_nvidia_toolkit || fail "Could not install the NVIDIA container toolkit."
  fi
  if ! grep -qs '"nvidia"' /etc/docker/daemon.json; then
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker
  fi
fi
systemctl enable --now docker
docker --version

# containers must not read user_data/metadata
iptables -C DOCKER-USER -d 169.254.169.254/32 -j DROP 2>/dev/null || iptables -I DOCKER-USER -d 169.254.169.254/32 -j DROP

# 4) worker software (only the changed layers are pulled when the prepared image is a bit stale)
stage downloading_software "$IMAGE"
if ! retry docker pull "$IMAGE"; then
  docker image inspect "$IMAGE" >/dev/null 2>&1 || fail "Could not download the worker software ($IMAGE)."
  say "warning: download failed, using the copy already on this machine"
fi
if [ "$GPU" = nvidia ]; then
  say "checking that containers can use the GPU"
  if ! GPU_INFO="$(docker run --rm --gpus all --entrypoint nvidia-smi "$IMAGE" -L 2>&1)"; then
    echo "$GPU_INFO"
    fail "The GPU can't be used inside containers: $(echo "$GPU_INFO" | tail -n1 | cut -c1-200)"
  fi
fi

# 5) hand over to the worker agent ---------------------------------------------------------
stage starting_engine
docker rm -f aivo-worker >/dev/null 2>&1 || true
VOL_ARGS=""
if [ -n "$VOLUME_MOUNT" ] && mountpoint -q "$VOLUME_MOUNT"; then VOL_ARGS="-v $VOLUME_MOUNT:$VOLUME_MOUNT:ro"; fi
python3 "$STATUS_PY" stop # frees the port for the agent
# shellcheck disable=SC2086
if ! docker run -d --name aivo-worker --restart unless-stopped $GPU_ARGS --shm-size 16g \
  -p "$PORT:$PORT" \
  -v /etc/aivo:/etc/aivo:ro \
  -v /var/lib/aivo/models:/var/lib/aivo/models \
  -v /run/aivo/jobs:/run/aivo/jobs \
  -v "$LOG:/var/log/aivo-bootstrap.log:ro" \
  $VOL_ARGS \
  -e AIVO_WORKER_BOOTSTRAP=/etc/aivo/bootstrap.json \
  -e AIVO_SETUP_LOG=/var/log/aivo-bootstrap.log \
  "$IMAGE"; then
  docker rm -f aivo-worker >/dev/null 2>&1 || true
  start_status_server
  fail "Could not start the worker software (see the log above)."
fi
bg_run aivo-boot-watch bash "$SELF" watch
say "bootstrap done $(date -Is); the worker agent takes over"
