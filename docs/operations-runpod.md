# Running on RunPod Serverless

aivo runs every creation on RunPod Serverless. You pay per second while a GPU works on a creation
(plus a short wait for a next one), and nothing while nobody creates anything.

## How it works

**One queue endpoint per GPU type.** The first time a creation needs a GPU type (say an L40S), aivo
creates a *queue-based* RunPod endpoint for it, named `aivo-<deployment>-l40s`. It keeps that
endpoint afterwards: an endpoint without running workers costs nothing, and RunPod keeps a worker
prepared on a host for it, so later starts are quicker.

**RunPod manages the workers.**
- A creation goes into its GPU type's queue as a RunPod job.
- RunPod starts a worker when a job waits, up to *GPUs of one type at the same time* (Settings → GPUs).
  Each worker takes one job at a time.
- After a job, the worker waits *Keep a GPU waiting for a next creation* for another job. Then it stops.
- If a worker crashes, RunPod retries the job on another one.

**The job is only a ticket; everything else goes straight between the worker and aivo:**

| Step | What happens | Billed |
|---|---|---|
| Queue | aivo submits a job whose whole input is a ticket: aivo's address, the job id and a one-time token. | No |
| Start | RunPod starts a worker on a host that already has the worker image. It's the same image as aivo's own release. | Yes, from here |
| Fetch | The worker asks aivo for the job with the ticket. It gets the engine, the model files and where to get them, and the starting picture. | Yes |
| Models | The worker downloads the model files from Hugging Face. They stay on the worker's disk for its next job, or on your network volume. | Yes |
| Improve | A small language model on the same GPU details the description (*Improve my description*); aivo checks the result and keeps it, sealed, with the creation. | Yes |
| Create | ComfyUI makes the creation. The worker reports progress and log lines to aivo. | Yes |
| Deliver | The worker uploads each result to aivo over HTTPS. aivo encrypts it onto the media volume right away, and the worker deletes its copy. | Yes |
| Done | The worker tells aivo it's done; the ticket stops working. RunPod's record of the job holds only file names. | Yes, while it waits for a next job |

**Nothing personal is stored at RunPod.**
- RunPod's job system never sees a prompt, a picture or a result, so there's no size limit on results either.
- The endpoint settings hold no secrets, only aivo's deployment id and the GPU type.

**When no GPU is free.** RunPod's stock changes all the time. If no worker took the job within *Try
another GPU type if none is free after* (default 5 min), aivo takes it out of that queue and puts it
in the next-best GPU type's queue. Waiting in a queue costs nothing.

**What it costs.** Each worker is billed from its start until it stops: its start-up, its jobs, the
waits between jobs, and the wait after the last one.
- **Spending** counts each worker that way.
- **Each creation** shows its share: the time since the worker's previous job ended (or since it started).

## Setup

1. **Create an API key** in the RunPod console (Credentials → API Keys). It can manage everything in
   your RunPod account, so keep it as secret as any cloud token.
   - **All** works.
   - A **Restricted** key must be allowed to create, change and delete Serverless endpoints *and
     their templates*, and to run jobs on endpoints it creates. A key that can create endpoints but not
     run jobs gets "403: you do not have permission to perform this action" from `api.runpod.ai`.
2. **Add credit.** RunPod is prepaid; with $0 balance, workers stop.
3. **Make aivo reachable for the workers.** They call `https://<your aivo>/api/v1/worker/...`.
   - Set `AIVO_PUBLIC_URL`, or `AIVO_WORKER_API_URL` if workers should use another address. It must be
     HTTPS with a public certificate.
   - Allow large request bodies on that path: up to the size of your longest video. With Helm and
     ingress-nginx, set `nginx.ingress.kubernetes.io/proxy-body-size: "4g"` (see `values.yaml`).
     The bundled frontend proxy already allows 4 GB there.
   - Only that path is used, and every call needs the job's ticket.
4. **Configure aivo.**
   - **Compose:** in `deploy/compose/.env`, set `AIVO_PROVIDER=runpod`, `AIVO_RUNPOD_API_KEY` and
     `AIVO_PUBLIC_URL`.
   - **Helm:** set `secrets.runpodApiKey` (or `AIVO_RUNPOD_API_KEY` in your existing Secret),
     `ingress.enabled` and `ingress.host`, or `publicUrl`. `provider: runpod` is the default.
5. Open **GPUs** in the dashboard. The options list RunPod's GPU types with their Serverless prices. The
   first creation creates the first endpoint.

| Setting | Default | Meaning |
|---|---|---|
| `AIVO_RUNPOD_COUNTRIES` | NL, BE, DE, FR, CZ, RO, SE, NO, IS, FI, PL, ES, IT, AT, DK, IE, PT | Countries (ISO codes) where workers may run |
| `AIVO_RUNPOD_DATA_CENTERS` | – | Exact data centers, e.g. `EU-NL-1,EU-RO-1`. Overrides the countries |
| `AIVO_RUNPOD_GPU_TYPES` | all | Only these GPU types, e.g. `NVIDIA L40S,NVIDIA RTX A5000` |
| `AIVO_RUNPOD_NETWORK_VOLUME_ID` | – | Keeps model files between workers (see below) |
| `AIVO_RUNPOD_DISK_GB` | 150 | Container disk for model files without a network volume |
| `AIVO_WORKER_API_URL` | `AIVO_PUBLIC_URL` | Where workers reach aivo |
| `AIVO_WORKER_IMAGE` | the release's `aivo-worker:<version>` | Pin another worker image |

In **Settings → GPUs**:

| Setting | Default | Meaning |
|---|---|---|
| GPUs of one type at the same time | 2 | The endpoint's max workers |
| Keep a GPU waiting for a next creation | 60 s | The endpoint's idle timeout: billed, but the next creation starts without a cold start |
| Try another GPU type if none is free after | 5 min | Moves a waiting job to the next GPU type |
| Give up waiting for a free GPU after | 30 min | Then the creation fails with "No GPU became available" |
| Longest a single creation may take | 60 min | Per picture batch or video: a creation of 4 videos may take 4× as long. RunPod's execution timeout gets an extra 20 min for model downloads |
| Never use these GPU types | – | RunPod GPU type names |

**GPUs → Keep one ready 30 min** sets that GPU type's minimum workers to 1 for half an hour. One
worker then stays on, and is billed the whole time, so creations start without a cold start.

RunPod's account limit on workers counts the *max workers* of all endpoints together, and starts at 5.
With 2 per GPU type, two GPU types use 4. aivo deletes endpoints only when they're not its own
anymore, so lower *GPUs of one type at the same time* if you hit the limit. Creations then wait with
"Your RunPod account's worker limit is reached". You can raise the limit in the RunPod console once
your balance qualifies.

## Model files: download each time, or a network volume

Without a network volume, a new worker downloads its model files from Hugging Face: about 1–3 billed
minutes for a 20–50 GB video model. A worker that stays on between creations keeps them. For
occasional use that's usually fine.

With a network volume, files are downloaded once and reused by every later worker:

1. Create a network volume in the RunPod console, in the data center you want (e.g. EU-NL-1). Size it
   for the engines you use, e.g. 200 GB at $0.07/GB-month ≈ $14/month.
2. Set `AIVO_RUNPOD_NETWORK_VOLUME_ID=<volume id>`. aivo updates its endpoints to mount it.

All workers then run in that one data center, so there are fewer GPUs to choose from. Only model
files are stored on the volume, never creations.

## Privacy and security

- **Creations and prompts never pass through RunPod's job system.** The worker fetches the prompt and
  starting picture from aivo, and uploads results to aivo. RunPod only holds the ticket (useless once
  the job ends) and the file names.
- **On the worker,** creations stay in memory (`/dev/shm`) where that is big enough, otherwise on the
  worker's temporary disk, and are deleted right after delivery. RunPod runs the GPU, so this
  trusts RunPod as much as any GPU provider.
- **Tickets.**
  - One per job; aivo stores only its SHA-256.
  - It opens only that job's `spec`, `inputs`, `prompt`, `progress`, `outputs`, `complete` and `fail`, and only
    while the job waits for or is with a worker.
  - It stops working when the job ends, is cancelled or moves to another GPU type.
  - A wrong ticket and an unknown job look the same (401).
  - An upload must match the checksum the worker sends with it.
- **Model files** come from Hugging Face. For gated models, aivo resolves a short-lived download link
  with its own token, so workers never hold the token.

## Troubleshooting

- **Show log.** On a creation, admins see a log button. It shows the worker's lines: model downloads,
  the job, the AI engine's output. The last 300 lines are kept with the creation.
- **A creation waits at "Starting a GPU".**
  - No GPU of that type is free in the allowed data centers. After 5 minutes aivo tries the next type.
  - Allow more countries or data centers, or more GPU types.
- **"Your RunPod account's worker limit is reached".** See the worker limit above.
- **Workers start but nothing arrives in aivo.**
  - The worker can't reach `AIVO_PUBLIC_URL`. Check the endpoint's worker logs in the RunPod console
    (Serverless → `aivo-…` endpoint → Workers): look for "aivo unreachable" or "HTTP 401/413".
  - A 413 means the ingress refuses large uploads.
- **401 errors in the backend log about RunPod.** The API key is wrong.
- **403 "you do not have permission" when submitting a creation**, although endpoints get created: the key
  may not run jobs on the endpoints it creates. Use a key with *All* permissions, or widen it (see Setup).
- **A creation fails with "most likely for running out of memory"** on a 48 GB GPU type: RunPod gives some
  types little RAM (an A40 worker gets 50 GB). aivo then tries a bigger GPU type.
- **`aivo inventory`** lists this deployment's endpoints with their queue and workers.

## Limits

- Only NVIDIA GPUs: the worker image is built for CUDA.
- RunPod's API was implemented from its documentation (API v2, November 2026) and is tested against a
  simulated RunPod. Against RunPod itself (November 2026), these were tried: the GPU catalog, creating
  and deleting endpoints, submitting jobs and reading their status, and the worker image running
  every engine. The whole loop, with workers calling a deployed aivo back, hasn't been tried yet.
  Please report anything that behaves differently.
