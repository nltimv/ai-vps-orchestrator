# ai-vps-orchestrator (aivo)

Create **images, videos and music with open AI models** on cloud GPU machines that exist
**only while there is work**. A GPU droplet is started when someone presses *Create*, is reused
while more work arrives, and is **destroyed** after it has been idle for a while. Powered-off
droplets are still billed, so destroying them is the only way to stop the meter.

- **Friendly dashboard**: describe what you want, pick a style, shape, quality and length.
  No samplers, steps or other jargon. You see a cost and wait estimate before you start.
- **Encrypted gallery** on your own volume. Generated content never stays at the cloud provider.
- **Automatic machine choice**: available GPU types and locations are ranked by suitability
  (enough memory but not overkill, price, availability, distance, already-running machines,
  cached models). The top one is used and the next one is tried when a type is sold out.
  You can also pick a machine by hand.
- **Fast start-up**: a prepared machine image, a private model mirror and optional model disks
  near the GPUs.
- **Safety nets against surprise bills**: idle shutdown, a maximum lifetime, monthly spending
  limits, orphan sweeps, an optional self-destruct watchdog on each machine, and a Kubernetes
  janitor.
- **DigitalOcean GPU Droplets**, or **RunPod Serverless** (experimental: pay only while a worker runs), behind a provider interface for other clouds.
- **Sign in with your own SSO** (OpenID Connect: Keycloak, Authentik, Entra ID, Google, …), with
  group-based admin roles and invite-only mode, alongside or instead of passwords
  ([docs/sso.md](docs/sso.md)).

```
Browser ─► frontend (React) ─► backend API ──► Postgres
                                   │           encrypted media volume  ◄── the only place content is kept
                                   └─ controller ──HTTPS (pinned per-machine certificate)──► GPU droplet
                                                                                             worker → ComfyUI
```

## Quick start (local, no cloud spend)

The default *fake* provider simulates GPU machines with a real worker agent that returns
placeholder images, videos and audio. The whole flow works on a laptop.

```bash
cp deploy/compose/.env.example deploy/compose/.env
```

```bash
docker compose -f deploy/compose/docker-compose.yml run --rm backend aivo generate-key
```

Put the printed key in `AIVO_MEDIA_KEKS` in `deploy/compose/.env`. **Back it up**: without it
the gallery cannot be decrypted.

```bash
docker compose -f deploy/compose/docker-compose.yml up --build
```

Open <http://localhost:5173> and create the admin account.

To use real GPUs, set `AIVO_PROVIDER=digitalocean` and `AIVO_DO_TOKEN` in `.env`. See
[docs/operations-do.md](docs/operations-do.md) first: it covers the GPU quota, a dedicated
team, billing alerts and the smoke test.

To try RunPod Serverless instead, set `AIVO_PROVIDER=runpod` and `AIVO_RUNPOD_API_KEY`. See
[docs/operations-runpod.md](docs/operations-runpod.md) for how it works, what it costs and
what's different.

## Production (Kubernetes)

Every merge to `main` is a release, versioned `<VERSION>.<build number>` (e.g. `0.2.57`). Its
images and its chart get that same version. Install a specific release from the published chart:

```bash
helm install aivo oci://ghcr.io/nltimv/charts/ai-vps-orchestrator --version 0.2.57 \
  --set secrets.mediaKeks="$(uv run aivo generate-key)" \
  --set secrets.doToken=dop_v1_... \
  --set ingress.enabled=true --set ingress.host=aivo.example.com
```

Upgrade by naming the new version. Dashboard, controller and the worker software on new GPU
machines all move to that release, and database migrations run before the new backend starts.
See [docs/releases.md](docs/releases.md).

The default topology runs API and controller in one pod, so it works with a ReadWriteOnce
media volume. It also includes a bundled PostgreSQL (official image), a janitor CronJob and
NetworkPolicies. See [`values.yaml`](deploy/helm/ai-vps-orchestrator/values.yaml).

## Which models?

| Dashboard name | Model | License | Typical machine |
|---|---|---|---|
| Image · Fast | Z-Image-Turbo | Apache-2.0 | RTX 4000 Ada (20 GB, ~$0.76/h) |
| Image · Best | FLUX.2 [dev] | Non-commercial | L40S / RTX 6000 Ada (48 GB) |
| Image · Text & posters | Qwen-Image-2512 | Apache-2.0 | 48 GB |
| Edit an image | Qwen-Image-Edit-2511, FLUX.2 [klein] 4B | Apache-2.0 | 48 GB / 20 GB |
| Video · Quick with sound | LTX-2 (19B, distilled) | LTX community (free < $10M revenue) | 48 GB (H100 for speed) |
| Video · Cinematic | Wan 2.2 A14B + 4-step speed-up | Apache-2.0 | 48 GB (H100 for speed) |
| Video · Budget | Wan 2.2 TI2V-5B | Apache-2.0 | 20 GB |
| Music · Song | ACE-Step 1.5 | MIT | 20 GB |
| Image · Unrestricted (18+) | Chroma1-HD | Apache-2.0 | 20 GB |
| Video · Animate (18+) | Wan 2.2 A14B + community adult add-on | add-on: no license stated | 48 GB |

The two 18+ engines are off until an admin switches adult content on for specific people; see
[docs/models.md](docs/models.md#adult-content-18) for the rules (never minors, never uploaded photos).

[docs/models.md](docs/models.md) explains the choices, the hardware tiers, what was left out
and how to add a model.

## Repository layout

| Path | What |
|---|---|
| `backend/` | FastAPI API, controller (scheduler, provisioner, reconciler, budget), providers, encrypted store |
| `worker/` | GPU worker agent (TLS API in front of headless ComfyUI) + CPU mock engine |
| `packages/protocol/` | Shared backend↔worker contract and catalog schema |
| `catalog/` | Model manifests and ComfyUI workflow templates (adding a model is data, not code) |
| `frontend/` | React + Vite + Tailwind dashboard |
| `deploy/` | docker compose (dev) and Helm chart (production) |
| `scripts/do_smoke.py` | Cost-capped real-GPU smoke test on DigitalOcean |
| `docs/` | Architecture, models, costs, security, operations, extending |

## Development

```bash
uv sync
```

```bash
AIVO_TEST_DATABASE_URL=postgresql+asyncpg://user@localhost/aivo_test uv run pytest
```

```bash
uv run ruff check backend worker packages
```

```bash
cd frontend && npm ci && npm run typecheck && npm run lint:jargon && npm run build
```

The test suite covers the following with real TLS mock workers and a real Postgres:

- **Crypto**: tampering, truncation, Range reads and key rotation.
- **The suitability scorer**: golden and property tests.
- **The DigitalOcean client**: mocked HTTP.
- **RunPod Serverless**: the full stack against a local stand-in for RunPod's API and gateway.
- **The ComfyUI engine**: against a fake ComfyUI.
- **The full stack**: capacity fallback, unknown create outcomes, orphans, out-of-memory retry,
  budget races, restart recovery, lease fencing and data residency.

## Status

The orchestration, dashboard, encryption, provider and deployment code are complete and
tested end to end with the simulated provider.

Not yet verified on real GPUs:

- The ComfyUI workflow templates. Verified so far on a real H100: Video · Budget (Wan 2.2 5B) and
  Image · Best (FLUX.2 [dev]).
- Model file names and sizes are checked against Hugging Face with `aivo catalog-check --online`.

Run `scripts/do_smoke.py` per engine before relying on one. Check each model's license
on its card before commercial use.
