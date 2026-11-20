# Models and hardware

This page explains which models/pipelines aivo offers, why, and on which DigitalOcean GPUs they run.

## DigitalOcean GPU tiers (Nov 2026)

| Size | GPU | VRAM | ~Price/h | Where | Notes |
|---|---|---|---|---|---|
| `gpu-4000adax1-20gb` | RTX 4000 Ada | 20 GB | $0.76 | TOR1 | cheapest; fast images, music, budget video |
| `gpu-6000adax1-48gb` | RTX 6000 Ada | 48 GB | $1.57 | TOR1 | **best value**: runs every engine in its compact (fp8) form |
| `gpu-l40sx1-48gb` | L40S | 48 GB | $1.57 | TOR1 | same tier as RTX 6000 Ada |
| `gpu-h100x1-80gb` | H100 | 80 GB | $4.41 | NYC2, AMS3, TOR1 | ~2–3× faster; full-precision variants; 5 TiB scratch disk |
| `gpu-h200x1-141gb` | H200 | 141 GB | $4.47 | NYC2, ATL1 | like H100, more memory |
| `gpu-mi300x1-192gb` | AMD MI300X | 192 GB | $2.59 | ATL1 | needs a ROCm worker image (not built yet) |
| 8× sizes | | | $20–35+ | | excluded: far more than one generation needs |

Prices and regions are read live from the API (`/v2/sizes`), never hard-coded. Availability
changes a lot, which is why the scheduler ranks and falls back instead of pinning one machine
type or datacenter.

## The catalog

| Dashboard name | Model | Why this one | License |
|---|---|---|---|
| **Image · Fast** (Auto for Draft/Standard) | Tongyi **Z-Image-Turbo** 6B | Photo quality in ~8 steps; fits 20 GB; seconds per image | Apache-2.0 |
| **Image · Best** (Auto for Best) | **FLUX.2 [dev]** 32B (fp8 on 48 GB, full on 80 GB) | Top open-weight quality and prompt adherence | **Non-commercial**: personal use |
| **Image · Text & posters** | **Qwen-Image-2512** 20B (+ Lightning speed-up for Draft) | Best text rendering and complex layouts | Apache-2.0 |
| **Edit an image** | **Qwen-Image-Edit-2511**; quick: **FLUX.2 [klein] 4B** | Instruction-based edits that keep the rest intact | Apache-2.0 |
| **Video · Quick with sound** (Auto for Standard) | **LTX-2.5** (distilled) | Very fast, generates matching audio; 16–48 GB | LTX community license (free < $10M revenue) |
| **Video · Cinematic** (Auto for Best) | **Wan 2.2 A14B** + Lightx2v 4-step | Best open motion quality, 720p; text-to-video and image-to-video | Apache-2.0 |
| **Video · Budget** (Auto for Draft) | **Wan 2.2 TI2V-5B** | 720p on the cheapest GPU | Apache-2.0 |
| **Music · Song** | **ACE-Step 1.5** (turbo) | Full songs with vocals/lyrics or instrumentals in seconds; tiny VRAM | MIT |

**Rule of thumb:**

| Kind | Default machine | Faster option |
|---|---|---|
| Images and music | RTX 4000 Ada (20 GB) | 48 GB tier |
| Video | 48 GB tier | H100 at *Best* quality or with *Fastest* priority |

The suitability scorer decides this per request. It compares the cost of each machine with the
value of the waiting time, so cold-start time counts too. A machine that is already running
with the right model loaded nearly always wins.

### Left out (and why)
- **Wan 2.5 / 2.6 / 2.7 / 3.0**: hosted-only; no downloadable weights.
- **HunyuanVideo 1.5**: its license excludes the EU, UK and South Korea.
- **MusicGen**: clearly behind ACE-Step for songs, and its weights are non-commercial.
- **Candidates to evaluate**:
  - **Qwen-Image-2.1**: research license, currently the top open-weight leaderboard score.
  - **H3-Image**
  - **ACE-Step 1.5 XL-turbo**

## Caveats

- **Pin before production.** Model file names and sizes in the manifests come from the public
  repos and are approximate until pinned. `sha256` and `revision` should be set per file.
  Until then the worker skips exact size and checksum checks.
- **Verify the newest workflows.** The workflow graphs follow ComfyUI's native nodes for each
  model family. For **LTX-2.5** and **ACE-Step 1.5** (both 2026 releases), confirm the node
  names against the pinned ComfyUI version before first use. The manifests note this. Then run
  `scripts/do_smoke.py --model <id>` once per engine.
- **Check licenses yourself.** Re-check each model card before any commercial use.
  FLUX.2 [dev] is personal/non-commercial only. LTX-2.x needs a paid license above $10M yearly
  revenue.

## Adding a model

1. Create `catalog/models/<id>/manifest.yaml`. Copy an existing one and edit it.
   - **Files**: Hugging Face repo, path, destination folder and size.
   - **Variants**: memory needs, with optional per-variant workflow parameters.
   - **Quality presets**: map Draft/Standard/Best to concrete parameters and sizes.
   - **Benchmark seconds** per GPU class: priors that measured timings replace over time.
   - **License** and a user-facing **tagline**.
2. Export the ComfyUI workflow in **API format** to `workflows/<mode>.json`. Replace the
   values you want to control with placeholders such as `"$prompt"`, `"$seed"`, `"$width"`,
   `"$height"`, `"$steps"`, `"$frames"`, `"$fps"`, `"$batch"`, `"$prefix"` and
   `"$reference_image"`. Any key from the quality preset or variant `params` also works.
3. Optionally add it to `catalog/routing.yaml` so *Auto* can pick it.
4. Run `uv run aivo catalog-check` and `uv run pytest backend/tests/unit/test_catalog.py`.
   Every combination must render.
5. Run a cost-capped smoke test on a real GPU: `scripts/do_smoke.py --model <id>`.

No worker rebuild is needed. The backend renders workflows and the worker just runs them.
