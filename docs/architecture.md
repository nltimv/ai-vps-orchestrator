# Architecture

```
Browser ──HTTPS──► frontend (React SPA via nginx) ──/api──► backend "aivo" (FastAPI)
                                                             roles: api | controller | all
                                                             ├─ Postgres: state, queue, lease, budget, telemetry
                                                             ├─ encrypted media volume (PVC / bind mount)
                                                             └─ HTTPS, per-machine pinned CA + SNI + bearer token
                                                                   ▼
                                                         GPU droplet: worker agent (:443) → ComfyUI (127.0.0.1)
                                                         job files on tmpfs; model weights on local NVMe
                                                         weights from: cache volume → Spaces mirror → Hugging Face
```

## Components
| Component | Where | Responsibility |
|---|---|---|
| **API** | `backend/src/aivo/api` | Auth, catalog, estimates, jobs, gallery and media (HTTP Range over encrypted files), machines, spending, settings, live events (SSE) |
| **Controller** | `backend/src/aivo/controller` | Holds the lease; scheduler, provisioner, job runner, reconciler, budget |
| **Placement** | `backend/src/aivo/placement` | Pure suitability scorer, estimates (priors → measured EWMAs), latency probes |
| **Providers** | `backend/src/aivo/providers` | `CloudProvider` interface; DigitalOcean; fake (local simulated machines) |
| **Storage** | `backend/src/aivo/storage` | Chunked AES-256-GCM envelope encryption, filesystem store, thumbnails |
| **Worker** | `worker/` | TLS agent, weight fetching, ComfyUI engine, output post-processing, watchdog |
| **Protocol** | `packages/protocol` | Pydantic contracts shared by backend and worker, catalog schema |

## Flow of one creation
1. **Create.** `POST /jobs` seals the prompt (field encryption) and queues a job. An event
   wakes the controller.
2. **Place.** The scheduler ranks running machines and live offers (`placement/scorer.py`):
   - An idle machine that fits gets the job right away.
   - If a fitting machine is starting or busy, the job waits for it.
   - Otherwise it reserves budget, creates an `instances` row (`pending`) with a ranked
     candidate list, and starts a provisioner.
3. **Provision.** The provisioner mints a throwaway CA and leaf certificate plus a 256-bit
   token, then calls `create_instance` with the per-deployment tags and a cloud-init bootstrap.
   - Capacity errors move to the next candidate.
   - An unknown outcome (a timeout) leads to a lookup by tag and adoption of any machine found.
     It never creates a duplicate.
   - The provisioner waits for an IP and then for the agent to report healthy over pinned TLS.
   - It rotates the token, because the copy in user_data becomes useless at that point.
   - The machine is then `ready`.
4. **Run.** The job runner:
   - asks the worker to fetch model files (volume → presigned Spaces URL → Hugging Face);
   - uploads the starting image, if any;
   - submits the rendered ComfyUI workflow and long-polls progress, relaying it as SSE.
5. **Store.** Outputs are downloaded into RAM scratch and checked against the worker's
   sha256. They are then encrypted onto the media volume, and thumbnails are made (also
   encrypted). Only after that does the worker delete its copies.
6. **Idle.** The idle clock starts only now. After `idle_timeout` (never before the provider's
   minimum billing time), the reconciler drains the machine and destroys it.

## Correctness
- **Compare-and-set state machines.** Every instance and job transition is
  `UPDATE … WHERE id=? AND state IN (…)`, and it proceeds only if exactly one row changed
  (`controller/lifecycle.py`).
- **Leadership vs. correctness.** A lease row with a fencing epoch elects one controller. It
  only avoids duplicate work; correctness does not depend on it. Destructive calls re-check
  the epoch.
- **Idempotent create.** The DB row is committed before the API call. Droplets are named and
  tagged with the instance UUID. On an unknown outcome the provisioner looks up by tag and
  adopts, rather than retrying.
- **Reconciler.** It only ever touches resources tagged with *this* deployment's id, so a
  laptop dev setup can't destroy production machines. It handles:
  - provider sync, plus orphan sweeps scoped by deployment tag;
  - idle, lifetime and stuck-start timeouts;
  - drain → destroy, cost accrual and budget enforcement.
- **Budget.** The month's `spend_periods` row is locked `FOR UPDATE` inside the placement
  transaction, so concurrent requests can't overshoot the cap.
- **Restart-safe.** Every in-flight job and machine is resumed from the database. Worker job
  ids are idempotent.

## Data residency
Generated content (outputs, thumbnails, uploaded starting images, prompts and lyrics) is
**persisted only on the backend's encrypted media volume**.
- **Cloud provider storage** (snapshots, the Spaces bucket, cache volumes) holds only public
  model weights and the worker software image.
- **On the GPU machine**, job input and output directories are tmpfs (RAM). The backend
  deletes them as soon as it has stored the files, and the machine is destroyed after use.
- **user_data** carries only the machine bootstrap (TLS key, token hash, image reference),
  never prompts. ComfyUI runs with `--disable-metadata`, and outputs are stripped again
  before transfer.

## Extending to another cloud
See [providers.md](providers.md).
