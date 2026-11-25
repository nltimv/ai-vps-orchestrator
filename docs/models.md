# Models and hardware

This page explains which models/pipelines aivo offers, why, and on which RunPod Serverless GPUs they run.

## RunPod Serverless GPU tiers (Nov 2026)

| GPU types (pool) | VRAM | ~Price/h while working | Notes |
|---|---|---|---|
| RTX A4000 / A4500 / RTX 4000 Ada | 16–20 GB | $0.58 | cheapest; fast images, music, budget video |
| L4 / RTX A5000 / RTX 3090 | 24 GB | $0.68–0.75 | the same jobs, a bit more memory |
| RTX 4090 | 24 GB | $1.12 | fast for images |
| RTX A6000 / A40 | 48 GB | $1.22 | older: no fast FP8, slower for FP8 video models |
| L40 / L40S / RTX 6000 Ada | 48 GB | $1.91 | **best value for video**: every engine in its compact (fp8) form |
| A100 | 80 GB | $2.74 | |
| H100 | 80 GB | $4.18 | ~2–3× faster; full-precision variants |
| RTX PRO 6000 | 96 GB | ~$4.00 | |

Prices and stock are read live from RunPod's GPU catalog, never hard-coded. Stock changes a lot,
which is why the scheduler ranks GPU types and moves a job to the next one when no GPU becomes free.

## The catalog

| Dashboard name | Model | Why this one | License |
|---|---|---|---|
| **Image · Fast** (Auto for Draft/Standard) | Tongyi **Z-Image-Turbo** 6B | Photo quality in ~8 steps; fits 20 GB; seconds per image | Apache-2.0 |
| **Image · Best** (Auto for Best) | **FLUX.2 [dev]** 32B (fp8 on 48 GB, full on 80 GB) | Top open-weight quality and prompt adherence | **Non-commercial**: personal use |
| **Image · Text & posters** | **Qwen-Image-2512** 20B (+ Lightning speed-up for Draft) | Best text rendering and complex layouts | Apache-2.0 |
| **Edit an image** | **Qwen-Image-Edit-2511**; quick: **FLUX.2 [klein] 4B** | Instruction-based edits that keep the rest intact | Apache-2.0 |
| **Video · Quick with sound** (Auto for Standard) | **LTX-2** 19B (distilled) | Very fast, generates matching audio; 16–48 GB | LTX community license (free < $10M revenue) |
| **Video · Cinematic** (Auto for Best) | **Wan 2.2 A14B** + Lightx2v 4-step | Best open motion quality, 720p; text-to-video and image-to-video | Apache-2.0 |
| **Video · Budget** (Auto for Draft) | **Wan 2.2 TI2V-5B** | 720p on the cheapest GPU | Apache-2.0 |
| **Music · Song** | **ACE-Step 1.5** (turbo) | Full songs with vocals/lyrics or instrumentals in seconds; tiny VRAM | MIT |
| **Image · Unrestricted (18+)** | **Chroma1-HD** 8.9B (FLUX.1 [schnell] based) | Trained without content filtering; realistic and illustrated; fits 20 GB in fp8 | Apache-2.0 |
| **Video · Animate (18+)** | **Wan 2.2 A14B** image-to-video + a community adult add-on | Animates a picture made here; the add-on is the most-used general one for Wan 2.2 | Wan: Apache-2.0; add-on: no license stated, personal use only |

**Rule of thumb:**

| Kind | Default GPU | Faster option |
|---|---|---|
| Images and music | 16–24 GB | 48 GB tier |
| Video | 48 GB tier | H100 at *Best* quality or with *Fastest* priority |

The suitability scorer decides this per request. It compares the cost of each GPU type with the
value of the waiting time, so cold-start time counts too. A GPU type with a worker already on
(waiting for a next creation) starts without a cold start and usually wins.

### Left out (and why)
- **Wan 2.5 / 2.6 / 2.7 / 3.0**: hosted-only; no downloadable weights.
- **HunyuanVideo 1.5**: its license excludes the EU, UK and South Korea.
- **MusicGen**: clearly behind ACE-Step for songs, and its weights are non-commercial.
- **Candidates to evaluate**:
  - **Qwen-Image-2.1**: research license, currently the top open-weight leaderboard score.
  - **H3-Image**
  - **ACE-Step 1.5 XL-turbo**

## Adult content (18+)

Off by default. Two switches are both needed, admins included: **Settings → Adult content** and
**18+** per person under **People**.

- **Who sees what.** Only those people see the 18+ engines and anything made with them (or made
  from such a picture with a normal engine). For everyone else, adult creations don't exist: they
  are not in the gallery or lists, and their pages and files answer "not found". Switching adult
  content off hides them from everyone.
- **Never real people.** The 18+ engines only start from pictures made here from a text
  description: never uploaded photos, nor edits of them.
- **Never minors.** Descriptions for adult creations are checked for words and ages that point at
  minors (English, Dutch and a few other languages) and refused. Every adult engine also adds
  "child, minor, teen, childlike, young-looking" to what it avoids. The check is deliberately broad:
  rewording a refused description is easy, a wrong creation can't be undone.
- **Auto never picks** an 18+ engine; you choose it under *More options → Engine*. To animate a
  picture, open it in the gallery and use *Animate this*: the 18+ animation engine is preselected
  for 18+ pictures.

## Caveats

- **Check the files.** `aivo catalog-check --online` checks every model file on Hugging Face:
  that it exists, its size, and whether it needs a token. Run it after changing a manifest.
  `sha256` and `revision` should still be pinned per file; until then the worker skips exact
  size and checksum checks.
- **Gated models.** Some repos (FLUX.2 [dev] full precision, LTX-2.5) require accepting a license
  on huggingface.co and a token (`AIVO_HF_TOKEN`). Without a token, aivo never picks a variant
  whose files are gated.
- **LTX-2.5** is not offered yet: its repo is gated and its files are split differently
  (22B transformer, Gemma 4 text encoder, separate video and audio VAEs), so it needs its own
  workflow. "Quick with sound" uses LTX-2 19B (public) meanwhile.
- **Verify the newest workflows.** The workflow graphs follow ComfyUI's native nodes for each
  model family. Confirm the node names for **ACE-Step 1.5** against the pinned ComfyUI version
  before first use. Then try each engine once on RunPod with a low spending limit.
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
5. Try it once on RunPod with a low spending limit (Settings → Spending limits).

No worker rebuild is needed. The backend renders workflows; the worker fetches and runs them.
