# Running on RunPod Serverless (experimental)

With `AIVO_PROVIDER=runpod`, aivo runs its GPU machines on RunPod Serverless instead of
DigitalOcean. Nothing changes for the people using aivo. What changes is billing: **you pay only
while a worker runs**, per second, with no minimum. Getting a GPU ready and downloading the worker
software is free.

This mode is new and has only been tested against a local stand-in for RunPod's API, not yet
against RunPod itself. Try it with a low spending limit first (Settings → Spending limits).

## How it works

Each aivo machine is its own RunPod **load-balancing endpoint** with at most one worker. The
worker runs aivo's normal worker image (`AIVO_WORKER_IMAGE`). It's the same agent and ComfyUI as
on DigitalOcean.

| Step | What happens | Billed |
|---|---|---|
| Start | aivo creates the endpoint (`aivo-<id>`) for one GPU type, limited to the allowed data centers. RunPod picks a host and pulls the worker image. The machine card shows "RunPod is preparing a GPU". | No |
| Boot | aivo's first request through RunPod's gateway (`https://<endpoint>.api.runpod.ai`) starts the worker. The agent comes up and starts ComfyUI. | Yes, from here |
| Work | The backend talks to the agent as on any other machine: it sends model downloads, the job and the starting picture, follows progress and fetches the results. Results are fetched in pieces of 16 MB because RunPod allows 30 MB per request. They go straight to aivo's encrypted volume. | Yes |
| Idle | aivo keeps the worker for follow-up creations for `AIVO_RUNPOD_KEEP_WARM_S` (2 min by default, or the general idle timeout if that's shorter). | Yes |
| Stop | aivo deletes the endpoint, which ends the worker at once. | Stops |

There are two safety nets:

- **If aivo goes silent**, RunPod stops the worker by itself after `AIVO_RUNPOD_IDLE_TIMEOUT_S`
  (90 s) without requests. Endpoints left behind cost nothing. aivo deletes its own leftover
  endpoints (named `aivo-…` and marked with its deployment id) when it comes back.
- **If a worker disappears mid-job** (RunPod moved or restarted it), every request is pinned to
  the worker that started the job (`X-Runpod-Worker-Id: strict …`). aivo notices at the next
  request and runs the job on a new worker.

## Setup

1. **Create an API key** in the RunPod console (Settings → API keys) with read/write access to
   Serverless. The key can manage everything in your RunPod account, so keep it as secret as a
   cloud token.
2. **Add credit.** RunPod is prepaid. With $0 balance, workers stop.
3. **Configure aivo.**
   - Compose: set these in `deploy/compose/.env`:
     ```
     AIVO_PROVIDER=runpod
     AIVO_RUNPOD_API_KEY=<the key>
     ```
   - Helm: set `provider: runpod` and `secrets.runpodApiKey`, or put `AIVO_RUNPOD_API_KEY` in
     your existing Secret. See the `runpod:` block in `values.yaml`.
4. Open Settings. The subtitle shows "Cloud: RunPod Serverless (experimental)", and the
   machine options list RunPod GPUs with their Serverless prices.

| Setting | Default | Meaning |
|---|---|---|
| `AIVO_RUNPOD_COUNTRIES` | NL, BE, DE, FR, CZ, RO, SE, NO, IS, FI, PL, ES, IT, AT, DK, IE, PT | Countries (ISO codes) where workers may run |
| `AIVO_RUNPOD_DATA_CENTERS` | – | Exact data centers, e.g. `EU-NL-1,EU-RO-1`. Overrides the countries |
| `AIVO_RUNPOD_GPU_TYPES` | all | Only these GPU types, e.g. `NVIDIA L40S,NVIDIA RTX A5000` |
| `AIVO_RUNPOD_NETWORK_VOLUME_ID` | – | Keeps model files between workers (see below) |
| `AIVO_RUNPOD_KEEP_WARM_S` | 120 | Keep an idle worker this long for follow-up creations (billed) |
| `AIVO_RUNPOD_IDLE_TIMEOUT_S` | 90 | RunPod stops a worker this long after the last request. Keep it above 30 s: aivo checks on idle workers every 30 s |
| `AIVO_RUNPOD_PREPARE_TIMEOUT_S` | 1200 | How long to wait for RunPod to prepare a GPU (free) before trying another GPU type |
| `AIVO_RUNPOD_DISK_GB` | 150 | Container disk for model files without a network volume |
| `AIVO_RUNPOD_REGION` | `eu` | Region name shown in aivo |

The account limit on workers across all endpoints starts at 5 at RunPod. Each aivo machine uses
one, so "Machines at the same time" (Settings) should stay at 5 or below. If the limit is hit,
the job waits with "Your cloud account's GPU limit is reached". You can raise the limit in the
RunPod console once your balance qualifies.

## Model files: download each time, or a network volume

Without a network volume, every new worker downloads its model files from Hugging Face again:
about 1–3 billed minutes for a 20–50 GB video model. That's fine for occasional use.

With a network volume, files are downloaded once and reused by later workers:

1. Create a network volume in the RunPod console, in the data center you want (e.g. EU-NL-1).
   Size it for the engines you use, e.g. 200 GB at $0.07/GB-month ≈ $14/month.
2. Set `AIVO_RUNPOD_NETWORK_VOLUME_ID=<volume id>`.

All workers then start in that one data center, which means fewer GPUs to choose from. Only
model files are stored on the volume, never creations.

## Privacy

- **Creations are never stored at RunPod.** They stay in the worker's memory (`/dev/shm`) where
  that is big enough; otherwise they go on the worker's temporary disk, which is wiped when the
  worker stops. Results are streamed to aivo's encrypted volume and deleted from the worker right
  away.
- **Traffic is encrypted, but RunPod's gateway decrypts it.** On DigitalOcean aivo pins each
  machine's own certificate. On Serverless, RunPod's gateway terminates HTTPS, so prompts,
  starting pictures and results pass through RunPod's gateway unencrypted, without being stored
  there. RunPod runs the GPU too, so this trusts the same party.
- **What RunPod stores.** The endpoint's settings (visible in the RunPod console) hold the
  worker's settings: machine ids and a hash of the worker's first token, which is replaced as
  soon as the worker is up. They never hold a secret or anything users made.
- **Who can call the worker.** Requests need your RunPod API key (checked by the gateway) and the
  worker's own aivo token (checked by the agent).
- Workers run in RunPod's Secure Cloud data centers in the countries you allow (EU by default).

## Costs compared

aivo's estimates know that preparing the GPU is free and that there's no minimum billing. For an
example with RunPod's November 2026 prices, a 5-second video on an L40S-class GPU:

| | Billed |
|---|---|
| DigitalOcean L40S: start, downloads, creation, 10 min idle | ≈ $0.50 |
| RunPod Serverless 48 GB PRO: start, downloads, creation, 2 min idle | ≈ $0.25 |

The gap grows when engines are reused: the worker's start-up (about 1 min for ComfyUI) is billed,
but pulling the image isn't. Spending shows what aivo counted per machine, from RunPod's list prices; your RunPod invoice is the final word.

## Troubleshooting

- **"RunPod had no GPU ready within 20 min"**: no worker became ready, often because the GPU type
  is sold out in the allowed data centers. aivo tries the next GPU type. Allow more countries or
  data centers, or more GPU types.
- **"RunPod could not start the worker software"**: the worker failed RunPod's health check for 4
  minutes. Look at the endpoint's worker logs in the RunPod console (Serverless → the `aivo-…`
  endpoint → Workers). Check that `AIVO_WORKER_IMAGE` is public and up to date.
- **Machine log** (Machines → Show log) works as on other providers once the worker is up. Before
  that, the card shows RunPod's preparation step.
- **401 errors in the backend log**: the API key is wrong or lacks Serverless access.

## Limits of this mode

- Prepared machine images, model disks and the firewall don't apply (Settings hides them). RunPod
  pulls the image itself, and access goes through the gateway.
- A starting picture can be at most 24 MB (RunPod's request size limit).
- Only NVIDIA GPUs (the worker image is built for CUDA).
- RunPod's API was implemented from its documentation (API v2, November 2026). It has been tested
  against a local stand-in, not yet against RunPod itself. Please report anything that behaves
  differently.
