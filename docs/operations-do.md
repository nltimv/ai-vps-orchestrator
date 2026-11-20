# Running on DigitalOcean

## 1. Account preparation

- **Use a dedicated team (or project) for aivo.** Everything aivo creates is tagged `aivo` and
  `aivo-dep-<deployment-id>`, and it only ever deletes resources carrying its own deployment
  tag. A dedicated team also limits what the optional watchdog token can do.
- **Check the GPU droplet limit.** New accounts often start with a GPU droplet limit of 0 or 1.
  Request an increase in the control panel if you want more than one machine at a time
  (Settings → *Machines at the same time*).
- **Create a billing alert** in the DO control panel as an independent last line of defence.
- **Create an API token** (`AIVO_DO_TOKEN`). Custom scopes needed:
  - droplet: create/read/delete
  - ssh_key: create/read
  - firewall: create/read/update
  - image and snapshot: read/update/delete (prepared images)
  - volume: create/read/delete (model disks)
  - action: read
  - size, region: read
  - account and billing: read (quota and the "cloud bill so far" figure)
- **Optional: a watchdog token** (`AIVO_DO_WATCHDOG_TOKEN`) with *only* `droplet:delete`.
  Each machine receives it and deletes itself if the backend stays silent for 20 minutes.
  This matters for a laptop backend that might sleep. Enable it in Settings → Security.

## 2. Configure

| Variable | Purpose |
|---|---|
| `AIVO_PROVIDER=digitalocean` | use real GPU droplets |
| `AIVO_DO_TOKEN` | API token |
| `AIVO_WORKER_IMAGE` | GPU worker image (built by CI: `ghcr.io/<owner>/aivo-worker`) |
| `AIVO_MEDIA_KEKS` | media encryption key(s): `aivo generate-key`; **back up separately** |
| `AIVO_SPACES_*` | optional private model mirror (bucket, region, key, secret) |
| `AIVO_HF_TOKEN` | optional: gated models (e.g. FLUX.2 [dev] full precision); used backend-side only |

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
  destroyed after 3 minutes.
- **Machine access.** A per-deployment Cloud Firewall allows port 443 only from the backend's
  public IP, which is detected automatically. On top of that, TLS is pinned to a per-machine
  certificate.
- **Inventory check.** `aivo inventory` lists every machine with this deployment's tag and its
  hourly price.
