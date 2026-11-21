# Running on DigitalOcean

## 1. Account preparation

- **Use a dedicated team (or project) for aivo.** Everything aivo creates is tagged `aivo` and
  `aivo-dep-<deployment-id>`, and it only ever deletes resources carrying its own deployment
  tag. A dedicated team also limits what the optional watchdog token can do.
- **Check the GPU droplet limit.** New accounts often start with a GPU droplet limit of 0 or 1.
  Request an increase in the control panel if you want more than one machine at a time
  (Settings → *Machines at the same time*).
- **Create a billing alert** in the DO control panel as an independent last line of defence.
- **Create an API token** (`AIVO_DO_TOKEN`) with custom scopes. See
  [API token scopes](#api-token-scopes) below.
- **Optional: a watchdog token** (`AIVO_DO_WATCHDOG_TOKEN`). Each machine receives it and
  deletes itself if the backend stays silent for 20 minutes. This matters for a laptop backend
  that might sleep. Enable it in Settings → Security. Its scopes are listed
  [below](#watchdog-token).

### API token scopes

Create the token under **API → Tokens → Generate New Token → Custom Scopes**. "Full Access"
works too, but isn't recommended.

**Required** (start, watch and destroy GPU machines):

| Scope | Used for |
|---|---|
| `droplet:create` | Start GPU machines (and the short-lived bake and disk-fill machines) |
| `droplet:read` | Poll a machine's status and IP; find machines by tag (safe retries, orphan cleanup) |
| `droplet:delete` | Destroy idle, expired or orphaned machines |
| `droplet:update` | Droplet actions: power-off and snapshot when preparing a machine image |
| `tag:create` | Machines are created with aivo's tags (`aivo`, `aivo-dep-…`, `aivo-inst-…`) |
| `sizes:read` | Live GPU sizes, prices and availability |
| `regions:read` | Which datacenters offer which sizes |
| `ssh_key:read`, `ssh_key:create` | Register a throwaway "no-login" key, so DigitalOcean never emails a root password |
| `firewall:read`, `firewall:create`, `firewall:update` | The per-deployment firewall that limits the worker port to the backend's IP |
| `actions:read` | Wait for droplet and image actions to finish |

**Optional, per feature.** Leaving these out only disables that feature; creating images,
videos and music keeps working.

| Feature | Scopes |
|---|---|
| Prepared machine image (Settings → Faster start-up) | `snapshot:read` (find the new snapshot), `image:read`, `image:update` (copy it to other regions), `image:delete` (remove old ones) |
| Model disks (block storage) | `block_storage:create`, `block_storage:read`, `block_storage:delete` |
| GPU quota check and the "Cloud bill so far" figure | `account:read`, `billing:read` |

#### Watchdog token

A separate token with only `droplet:delete`. The control panel may add `droplet:read`
automatically. Each machine gets a copy, and the token can delete **any** droplet in the
account or team. Only use it inside a dedicated team for aivo.

#### Notes

- **Companion scopes.** When you pick some scopes (e.g. `droplet:create`), the control panel
  automatically adds the read scopes they depend on. Keep those.
- **A missing scope** makes the API answer `403`.
  - aivo shows "The cloud account is not set up correctly (check the API token)".
  - The backend log contains DigitalOcean's message naming the missing scope.
- **Check a new token for free** with `scripts/do_smoke.py --dry-run`. It uses the read
  scopes only and costs $0.

## 2. Configure

| Variable | Purpose |
|---|---|
| `AIVO_PROVIDER=digitalocean` | use real GPU droplets |
| `AIVO_DO_TOKEN` | API token |
| `AIVO_WORKER_IMAGE` | GPU worker image (built by CI: `ghcr.io/<owner>/aivo-worker`) |
| `AIVO_DO_BASE_IMAGE` | Operating-system image for NVIDIA machines. Default `gpu-h100x1-base`: DigitalOcean's GPU image (Ubuntu 24.04 with the NVIDIA driver), which DigitalOcean documents for all single-GPU sizes. It **must** include the GPU driver; aivo never falls back to plain Ubuntu. |
| `AIVO_DO_BASE_IMAGE_AMD` | The same for AMD machines. Default `gpu-amd-base` |
| `AIVO_DO_SSH_KEY_IDS` | Debugging only: ids or fingerprints of your own DigitalOcean SSH keys, added to every machine. See [debugging](#6-machines-dont-start--debugging). |
| `AIVO_DATABASE_URL` | Postgres URL. A managed database's connection string can be pasted as given (e.g. DigitalOcean's `postgresql://…?sslmode=require`); the `postgresql+asyncpg://…?ssl=require` form works too. |
| `AIVO_MEDIA_KEKS` | media encryption key(s): `aivo generate-key`; **back up separately** |
| `AIVO_SPACES_*` | optional private model mirror (bucket, region, key, secret) |
| `AIVO_HF_TOKEN` | optional: gated models (e.g. FLUX.2 [dev] full precision). Accept the model's license on huggingface.co with the same account. Used backend-side only; without it, gated variants are never chosen. |

## 3. Smoke test (costs a little)
```bash
AIVO_DO_TOKEN=... uv run python scripts/do_smoke.py --dry-run
```
This ranks the GPU offers available right now and costs $0.

```bash
AIVO_DO_TOKEN=... uv run python scripts/do_smoke.py --max-usd 1.5 --i-understand-this-costs-money --report smoke.json
```
This starts one machine, generates one image and destroys the machine again. It prints timings
per phase (create, boot, model download, generation). A detached watchdog deletes the machine at
the cost deadline even if the script dies. Run it once per engine (`--model <id>`) before
relying on that engine.

## 4. Faster start-ups (Settings → Faster start-up)

| What | Saves | Cost |
|---|---|---|
| **Prepared machine image** (bake) | ~4–5 min of setup per start | ~$0.30 per bake + ~$0.06/GB-month per region (~$2/region) |
| **Model copy** (Spaces mirror) | faster, more reliable downloads than Hugging Face | $5/month for 250 GB |
| **Model disks** (block storage) | no download at all for the cached models | $0.10/GB-month (~$12 for 120 GB) |

**Prepared machine image:** the cheapest available GPU machine gets Docker, the NVIDIA
container toolkit and the worker image installed. It is then snapshotted and copied to the
regions you pick.

**Model copy (Spaces mirror):** the backend streams model files from Hugging Face into your
private bucket. Machines receive short-lived download links and never see your keys.

**Model disks:** a short-lived $0.03/h CPU machine fills each disk, then destroys itself. Each
disk is mounted read-only and serves one GPU machine at a time; other machines in that region
download from the Spaces copy instead. Keep the disks only if the measured start-up times
justify them.

The "Balanced" default (~$10–25/month) is:
- a prepared image in TOR1 + NYC2;
- the Spaces mirror;
- one ~120 GB model disk in TOR1, the only region with the 48 GB tier.

## 5. Day-to-day safety
- **Idle shutdown** after 10 minutes (configurable). It never fires before the provider's
  minimum billing time.
- **Maximum lifetime** of 4 hours per machine, with a grace period for the running job.
- **Monthly limits.** Above the warning level, users are asked to confirm. At the limit, new
  machines stop starting and running ones finish up. At the limit plus $2 they are destroyed.
- **Orphans.** Machines carrying this deployment's tag but unknown to the database are
  destroyed after 3 minutes. Exceptions: the machine preparing a machine image and the one filling
  a model disk, while that task runs (at most 3 h and 5 h).
- **Machine access.** A per-deployment Cloud Firewall allows port 443 only from the backend's
  public IP, which is detected automatically. On top of that, TLS is pinned to a per-machine
  certificate.
- **Inventory check.** `aivo inventory` lists every machine with this deployment's tag and its
  hourly price.
- **Start-up failures.** A machine that reports a definite problem (for example no GPU driver) is
  destroyed right away, and the job fails with the reason instead of trying machine after machine.
  A machine that stays silent gets one retry. After 2 failed start-ups in a row, new machines are
  paused until an admin resumes them (see below).

## 6. Machines don't start / debugging

### The machine log
Admins can open **Machines → Show log** on any running machine, and on recently shut down ones.
The log is live and combines four sources:

| Source | What it shows |
|---|---|
| **aivo** | The backend's own decisions: which GPU and region it asked for, sold-out fallbacks, start-up steps, timeouts, shutdown |
| **Start-up** | The machine's start-up script (`/var/log/aivo-bootstrap.log`) |
| **Agent** | aivo's worker program on the machine: model downloads, jobs received, finished, failed |
| **AI engine** | ComfyUI's own output, including errors and tracebacks |

You can filter by source, search, copy, and download it as a text file. When a machine shuts down, aivo
keeps its last 60 lines, so you can still read why it failed.

### Start-up steps
Until the worker program runs, a small status server on the machine (same port, same pinned
certificate and token) reports the current step:

1. **preparing disks**: the RAM disk for job files, the scratch disk, the model disk (if any)
2. **checking the GPU**: is there a working GPU driver?
3. **installing the container runtime**: Docker and the NVIDIA container toolkit (already present on a prepared image)
4. **downloading the worker software**: the worker image, ~8 GB the first time
5. **starting the AI engine**: the worker program starts ComfyUI

The machine card shows the current step, and so does a job that is waiting for its machine.

Each start-up gets 10 minutes from a prepared image, or 25 minutes from the plain GPU image. That
time is extended as long as the machine makes progress (a new step, or the download moving), up to
40 minutes in total. The limits are the `boot_health_timeout_s`, `boot_health_timeout_base_s`,
`boot_progress_grace_s` and `boot_health_max_s` settings.

### Common failures

| What the log says | Cause and fix |
|---|---|
| *This machine has an NVIDIA GPU but no GPU driver* | The machine was started from an image without GPU drivers. Leave `AIVO_DO_BASE_IMAGE` unset (default `gpu-h100x1-base`) or point it at a GPU image. Prepared images baked from a plain image have the same problem: delete them and bake again. |
| *The model storage on this machine (/var/lib/aivo/models) is read-only* | The disk meant for model files can't be written to. Before this check existed, the start-up script could mistake DigitalOcean's small read-only config drive (`/dev/vdb`) for a scratch disk; the AI engine then failed with `Read-only file system`. Now only a blank, writable disk of 100 GB or more is used as a scratch disk. |
| *The GPU can't be used inside containers* | The NVIDIA container toolkit isn't working. The message includes Docker's error. |
| *Could not download the worker software* | `AIVO_WORKER_IMAGE` is wrong or private, or the registry is unreachable. |
| *The worker software keeps stopping* | The worker container crashes on start. Its last output is in the Start-up lines. |
| *The AI engine could not start* | ComfyUI exited. The AI engine lines show its traceback. |
| *The machine never answered* | Nothing on the machine answered on the worker port. Either the start-up script didn't run (cloud-init), or the firewall doesn't allow the backend's address (Settings → Security). |
| *The cloud account is not set up correctly … (check AIVO_DO_BASE_IMAGE)* | DigitalOcean rejected the image name. |

### Paused start-ups
After 2 failed start-ups in a row, aivo stops starting new machines. Each retry would bill another
GPU machine for the same problem. While start-ups are paused:
- a banner on the Machines and Settings pages shows the reason;
- new jobs fail right away with that reason;
- machines that are already running keep working.

Fix the cause, then press **Resume start-ups**. A machine that starts successfully also clears the
pause. The number of failures is the `boot_failures_before_pause` setting; 0 turns the pause off.

### Logging in to a machine (optional)
Normally nobody can log in to the machines. To debug one by hand:
1. Add your own SSH key in DigitalOcean.
2. Set `AIVO_DO_SSH_KEY_IDS` to its id or fingerprint.
3. Put your public IP in the `debug_ssh_cidrs` setting. This opens port 22 in the deployment's
   firewall for that address only.
4. Start a machine and `ssh root@<ip>`. The setup log is in `/var/log/aivo-bootstrap.log`, and the worker
   runs in `docker logs aivo-worker`.

Remove both settings again afterwards.

### Testing the start-up script without a GPU
```bash
uv run python scripts/check_bootstrap.py
```
This runs `bootstrap.sh` and the status server in an Ubuntu 24.04 container (Docker required), with
stubs for the GPU tools. It checks the four main cases: no GPU driver, GPU not usable in containers,
a healthy start, and a crashing worker.

### Privacy
The log never contains prompts or tokens. aivo logs only ids, engine names and output sizes. The
log travels only over the pinned connection between the backend and the machine. Only admins can
see it, and it isn't stored, except the last 60 lines of a machine after it shuts down. One
exception: when ComfyUI reports an error about a specific input, its message can quote part of the
request.
