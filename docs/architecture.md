# Architecture

```
Browser ──HTTPS──► frontend (React SPA via nginx) ──/api──► backend "aivo" (FastAPI)
                                                             roles: api | controller | all
                                                             ├─ Postgres: state, queue, lease, budget, telemetry
                                                             ├─ encrypted media volume (PVC / bind mount)
                                                             └─ RunPod API: one queue endpoint per GPU type
                                                                   │  job input = ticket
                                                                   ▼
             /api/v1/worker/ (HTTPS, ticket) ◄───────────── RunPod worker → ComfyUI (127.0.0.1)
             fetch job · progress + log · upload results    job files in RAM; model files on disk
                                                             or a network volume; weights from Hugging Face
```

## Components
| Component | Where | Responsibility |
|---|---|---|
| **API** | `backend/src/aivo/api` | Auth, catalog, estimates, jobs, each user's gallery (items, favorites, collections) and media (HTTP Range over encrypted files), GPUs, spending, settings, live events (SSE) |
| **Worker API** | `backend/src/aivo/api/routes/worker.py` | What RunPod workers call with a job's ticket: the job, its starting picture, progress and log lines, results, done/failed |
| **Controller** | `backend/src/aivo/controller` | Holds the lease; scheduler, job runner, endpoints per GPU type, worker billing records, housekeeping, budget |
| **Placement** | `backend/src/aivo/placement` | Pure suitability scorer, estimates (priors → measured EWMAs) |
| **RunPod** | `backend/src/aivo/providers/runpod` | REST + queue API client, GPU catalog, a simulated RunPod for development and CI |
| **Storage** | `backend/src/aivo/storage` | Chunked AES-256-GCM envelope encryption, filesystem store, thumbnails |
| **Worker** | `worker/` | RunPod Serverless handler: fetches the job from aivo, model files, ComfyUI engine, output post-processing, delivers results |
| **Protocol** | `packages/protocol` | Pydantic contracts shared by backend and worker (ticket, spec, progress, results), catalog schema |

## Flow of one creation
1. **Create.** `POST /jobs` seals the prompt (field encryption) and queues a job. An event
   wakes the controller.
2. **Place.** The scheduler ranks RunPod's GPU types for the job (`placement/scorer.py`). It reserves
   budget, fixes the job's parameters (seed included) and hands the job to a job runner.
3. **Submit.** The job runner makes sure the GPU type's queue endpoint exists
   (`controller/endpoints.py`: created on first use, kept in step with settings and releases). It mints
   a one-time ticket, stores its hash, and submits a RunPod job whose input is only the ticket.
4. **Work.** RunPod starts a worker, or reuses one waiting on that endpoint. The worker:
   - fetches the job from aivo with the ticket (`/spec`): workflow, model files with download links,
     output shape;
   - downloads model files from Hugging Face, unless a volume has them already: from a global volume it
     copies them to its own disk in parallel ranges; a downloaded file is copied onto a global volume
     for later workers;
   - improves the description with a small language model in ComfyUI, as soon as its file is there,
     and sends the result to aivo (`/prompt`), which checks it and answers with the words to use;
   - fetches the starting picture, if any;
   - runs ComfyUI, reporting progress and log lines (`/progress`, relayed to browsers as SSE);
   - uploads each batch's results as soon as they're done, while it makes the next batch
     (`PUT /outputs/<name>`, checked against its SHA-256); the dashboard shows them right away;
   - says it's done (`/complete`).
5. **Store.** Each upload is encrypted onto the media volume as it arrives, and thumbnails are made
   (also encrypted). On `/complete` the job succeeds, the ticket is cleared, and the worker deletes its
   copies.
6. **Bill.** Each RunPod worker is an `instances` row: billed from its start until it stopped, which is
   `keep_warm_s` after its last job. Each job also gets its share: the worker's time since its
   previous job (or its start).

## When things go wrong
- **No worker takes the job** within `gpu_fallback_after_s`: the runner cancels it at RunPod and moves
  the job to the next GPU type on its list, with a new ticket. When the list is used up, the job waits
  and is placed again, until `capacity_wait_max_s`.
- **A worker dies mid-job:** RunPod retries the job on another worker. That worker fetches the job
  again (`/spec`), and anything the first one delivered is discarded.
- **Out of GPU memory:** the job goes back into aivo's queue with a higher memory floor, so it lands on
  a bigger GPU type.
- **The worker fails** (engine error, model files missing): the job fails with a plain message; the
  worker's log is kept with it for admins.
- **Stopped or failed after some results arrived:** those are kept, and the creation is done with fewer
  and a note saying why (e.g. "Stopped after 5 of 16, as you asked").
- **RunPod reports the job failed or timed out** without the worker telling aivo: the runner fails the
  job.
- **Cancel:** a job still waiting is cancelled at RunPod; a worker that has it learns at its next
  progress report and stops.

## Correctness
- **Compare-and-set state machines.** Every job transition is `UPDATE … WHERE id=? AND state IN (…)`,
  and it proceeds only if exactly one row changed (`controller/lifecycle.py`).
- **Leadership vs. correctness.** A lease row with a fencing epoch elects one controller. It only
  avoids duplicate work; correctness does not depend on it.
- **Tickets.** The ticket is valid before RunPod gets the job, and stops working when the job ends,
  moves to another GPU type or is cancelled. A late worker on an old ticket is turned away.
- **Housekeeping.** It only ever touches endpoints marked with *this* deployment's id (endpoint
  environment `AIVO_DEPLOYMENT_ID`), so a laptop dev setup can't delete production endpoints. It:
  - closes billing records of workers RunPod has stopped;
  - accrues costs and enforces the budget;
  - keeps endpoints in step with the settings and the release;
  - raises max workers again if RunPod lowered them for an endpoint unused for days;
  - deletes endpoints aivo no longer knows.
- **Budget.** The month's `spend_periods` row is locked `FOR UPDATE` inside the placement
  transaction, so concurrent requests can't overshoot the cap.
- **Restart-safe.** Every job with RunPod is resumed from the database; the worker keeps working and
  delivers to the API meanwhile.

## Data residency
Generated content (outputs, thumbnails, uploaded starting images, prompts and lyrics) is
**persisted only on the backend's encrypted media volume** and in sealed database fields. The
improved description is sealed like the description itself; the job's resolved parameters keep no
copy of either (the worker's workflow gets them filled in when it fetches the job).
- **RunPod's job system** only sees the ticket and file names: the worker fetches the prompt and
  picture from aivo and uploads results to aivo.
- **The endpoint settings** hold the worker image, aivo's deployment id and the GPU type. They hold no
  secrets.
- **On the worker**, job input and output directories are in RAM (`/dev/shm`) when it is big enough,
  otherwise on the worker's temporary disk. They are deleted right after delivery, and the worker
  itself stops after a short wait.
- **A global or network volume**, if configured, holds only public model files. ComfyUI runs with
  `--disable-metadata`, and outputs are stripped again before delivery.
