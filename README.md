# ai-vps-orchestrator (aivo)

Create **images, videos and music with open AI models** on **RunPod Serverless** GPUs that run
**only while there is work**. Each GPU type has a queue on RunPod: when someone presses *Create*,
the job goes into the best-fitting GPU's queue, RunPod starts a worker, the worker delivers the result
straight back to aivo, and stops shortly after. You pay per second of GPU work, nothing in between.

- **Friendly dashboard**: describe what you want, pick a style, shape, quality and length.
  No samplers, steps or other jargon. You see a cost and wait estimate before you start.
- **Encrypted gallery** on your own volume. Creations never stay at RunPod: workers upload them to aivo,
  which encrypts them right away. RunPod's job queue only ever sees a one-time ticket.
- **Automatic GPU choice**: RunPod's GPU types are ranked by suitability (enough memory but not
  overkill, price, availability, a GPU of that type already on). When no GPU of a type becomes free in
  time, the job moves to the next type. You can also pick a GPU by hand.
- **RunPod runs the workers**: one queue endpoint per GPU type, created on first use. RunPod starts
  workers for waiting jobs (up to your limit per type), reuses a worker for the next job while it waits,
  and stops it after a short wait. An idle endpoint costs nothing.
- **Safety nets against surprise bills**: monthly spending limits with per-job reservations, a time
  limit per creation, workers that stop on their own, and cleanup of endpoints aivo no longer uses.
- **Sign in with your own SSO** (OpenID Connect: Keycloak, Authentik, Entra ID, Google, …), with
  group-based admin roles and invite-only mode, alongside or instead of passwords
  ([docs/sso.md](docs/sso.md)).

```
Browser ─► frontend (React) ─► backend API ──► Postgres
                                 ▲   │          encrypted media volume  ◄── the only place creations are kept
                                 │   └─ controller ──► RunPod API: queue endpoint per GPU type, job = ticket
                                 │                                    │
                                 └── HTTPS, one-time ticket ◄──── RunPod worker → ComfyUI
                                     (fetch job, progress, upload results)
```

## Quick start (local, no cloud spend)

The default *fake* provider simulates RunPod inside the backend: queues, workers that start and
stop, and the real worker code with a CPU engine that returns placeholder images, videos and audio.
The whole flow works on a laptop.

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

To use real GPUs, set `AIVO_PROVIDER=runpod`, `AIVO_RUNPOD_API_KEY` and `AIVO_PUBLIC_URL` (an HTTPS
address RunPod's workers can reach) in `.env`. See [docs/operations-runpod.md](docs/operations-runpod.md)
for how it works, what it costs and how to set it up.

## Production (Kubernetes)

Every merge to `main` is a release, versioned `<VERSION>.<build number>` (e.g. `0.2.57`). Its
images and its chart get that same version. Install a specific release from the published chart:

```bash
helm install aivo oci://ghcr.io/nltimv/charts/ai-vps-orchestrator --version 0.2.57 \
  --set secrets.mediaKeks="$(uv run aivo generate-key)" \
  --set secrets.runpodApiKey="<your RunPod API key>" \
  --set ingress.enabled=true --set ingress.host=aivo.example.com
```

Upgrade by naming the new version. Dashboard, controller and the worker software RunPod runs all
move to that release, and database migrations run before the new backend starts.
See [docs/releases.md](docs/releases.md).

The dashboard must be reachable from the internet: RunPod's workers fetch jobs and deliver results
under `/api/v1/worker/` (allow large uploads on your ingress, see `values.yaml`). The default topology
runs API and controller in one pod, so it works with a ReadWriteOnce media volume. It also includes a bundled PostgreSQL (official image), a janitor CronJob and
NetworkPolicies. See [`values.yaml`](deploy/helm/ai-vps-orchestrator/values.yaml).

## Which models?

| Dashboard name | Model | License | Typical GPU |
|---|---|---|---|
| Image · Fast | Z-Image-Turbo | Apache-2.0 | 16–24 GB (~$0.58–0.68/h on RunPod) |
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
| `backend/` | FastAPI API, worker API, controller (scheduler, job runner, housekeeping, budget), RunPod client + simulated RunPod, encrypted store |
| `worker/` | RunPod Serverless worker (headless ComfyUI) + CPU mock engine |
| `packages/protocol/` | Shared backend↔worker contract and catalog schema |
| `catalog/` | Model manifests and ComfyUI workflow templates (adding a model is data, not code) |
| `frontend/` | React + Vite + Tailwind dashboard |
| `deploy/` | docker compose (dev) and Helm chart (production) |
| `docs/` | Architecture, models, costs, security, RunPod operations, releases |

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

The test suite runs the full stack against the simulated RunPod (real worker code, CPU engine) and a
real Postgres:

- **Crypto**: tampering, truncation, Range reads and key rotation.
- **The suitability scorer**: golden and property tests.
- **The RunPod client**: request paths and errors (mocked HTTP), and the endpoint aivo creates per GPU type.
- **The ComfyUI engine**: against a fake ComfyUI.
- **The full stack**: tickets, one endpoint per GPU type, workers reused and stopped, parallel jobs,
  falling back to the next GPU type, a crashed worker, out-of-memory retry, cancelling, budget races,
  restart recovery, lease fencing, adult-content rules and data residency.

## Status

The orchestration, dashboard, encryption and deployment code are complete and tested end to end with
the simulated RunPod. The RunPod API calls follow RunPod's documentation (API v2, November 2026) but
haven't run against RunPod itself yet: try it with a low spending limit first.

Not yet verified on real GPUs:

- The ComfyUI workflow templates. Verified so far on a real H100: Video · Budget (Wan 2.2 5B) and
  Image · Best (FLUX.2 [dev]).
- Model file names and sizes are checked against Hugging Face with `aivo catalog-check --online`.

Try each engine once with a low spending limit before relying on it. Check each model's license on
its card before commercial use.
