# Costs

## How the bill is built
- **GPU time on RunPod Serverless.** RunPod bills a worker per second from its start until it stops:
  - its start-up (container + AI engine);
  - its creations, including downloading model files;
  - the waits between creations;
  - the wait after the last one (*Keep a GPU waiting for a next creation*, default 60 s).

  Waiting in a queue for a free GPU costs nothing, and so does an idle endpoint.
- **Typical cost of a creation** (RunPod's November 2026 prices; the dashboard shows a live
  estimate):

  | Creation | GPU | Cost |
  |---|---|---|
  | Two Draft images (Z-Image Turbo, cold start) | 16–24 GB, ~$0.58–0.68/h | ~$0.02–0.04 |
  | Two Standard images (FLUX.2 [dev], cold start, model download) | 48 GB, ~$1.91/h | ~$0.10–0.20 |
  | A 5 s Standard video with sound (LTX-2, cold start, model download) | 48 GB, ~$1.91/h | ~$0.15–0.30 |
  | A 5 s Standard video with Wan 2.2 (full model, ~4 min of creating on an H100) | 80 GB, ~$4.18/h | ~$0.35–0.55 |
  | A follow-up creation on a GPU that's still waiting | — | only its own time |

  *Improve my description* adds about 10–30 s per creation (a small language model on the same GPU).
  Draft keeps the quick settings for when cost or speed matters more than detail.

- **Network volume** (optional, keeps model files): $0.07/GB-month, e.g. ~$14/month for 200 GB.
- **Keep one ready** (GPUs page): one worker stays on and is billed the whole time, e.g. 30 min of
  an L40S-class GPU ≈ $0.95.

## How the GPU is chosen
For each GPU type the scorer estimates:
- **wait** = time until a worker has the job (queue + cold start; ~0 when a worker of that type is
  on) + model load + generation + delivery;
- **cost** = price × the billed part of that time, plus the expected wait for a next creation.

It then minimizes `cost + λ × wait`. The λ values are what an hour of waiting is worth:

| Priority | λ ($ per hour of waiting) |
|---|---|
| Save money | $1 |
| Balanced | $4 |
| Fastest | $20 |

Small penalties are added for GPU types that often had no free GPU lately, for far more GPU than
needed, and for lighter model variants. Estimates start from priors and are replaced by the timings
measured on your own runs.

## Limits
- **Warning level** (`soft_cap_usd`, default $30/month): users confirm each new creation.
- **Spending limit** (`hard_cap_usd`, default $40/month):
  - Each creation reserves its expected cost atomically when it's placed, so concurrent requests
    can't overshoot.
  - At the limit, new creations aren't placed; running ones finish.
  - At the limit plus $2, running ones are cancelled.
- aivo's numbers come from RunPod's list prices and the workers' own timing. Your RunPod invoice is
  the final word; set a low balance alert in the RunPod console.
