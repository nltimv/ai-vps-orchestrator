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
| **Image · Fast** (Auto for Draft) | Tongyi **Z-Image-Turbo** 6B | Photo quality in ~8 steps; fits 20 GB; seconds per image | Apache-2.0 |
| **Image · Best** (Auto for Standard and Best) | **FLUX.2 [dev]** 32B (fp8 on 48 GB, full on 80 GB) | Top open-weight quality and prompt adherence | **Non-commercial**: personal use |
| **Image · Text & posters** | **Qwen-Image-2512** 20B (+ Lightning speed-up for Draft) | Best text rendering and complex layouts | Apache-2.0 |
| **Edit an image** | **Qwen-Image-Edit-2511**; quick: **FLUX.2 [klein] 4B** | Instruction-based edits that keep the rest intact | Apache-2.0 |
| **Video · Quick with sound** (Auto for Standard) | **LTX-2** 19B (distilled, two stages with a 2× upscaler) | Very fast, generates matching audio; 32–48 GB | LTX community license (free < $10M revenue) |
| **Video · Cinematic** (Auto for High and Best) | **Wan 2.2 A14B** (Lightx2v 4-step at Draft/Standard, a middle way at High, the full model at Best), smoothed to 32 fps | Best open motion quality, 720p; text-to-video and image-to-video | Apache-2.0 |
| **Video · Budget** (Auto for Draft) | **Wan 2.2 TI2V-5B** | 720p on the cheapest GPU | Apache-2.0 |
| **Music · Song** | **ACE-Step 1.5** (turbo) | Full songs with vocals/lyrics or instrumentals in seconds; tiny VRAM | MIT |
| **Image · Unrestricted (18+)** | **Chroma1-HD** 8.9B (FLUX.1 [schnell] based) | Trained without content filtering; realistic and illustrated; fits 20 GB in fp8 | Apache-2.0 |
| **Video · Animate (18+)** | **Wan 2.2 A14B** image-to-video + a community adult add-on | Animates a picture made here; the add-on is the most-used general one for Wan 2.2 | Wan: Apache-2.0; add-on: no license stated, personal use only |

## Choices

- **How many:** up to 8 pictures, 4 videos or 4 songs per creation. aivo makes them in batches the GPU
  can hold, one after another in the same job, each batch with its own look number.
- **Length:** any length in whole seconds, from 2 s up to what the engine can do (8–10 s for videos), and
  10 s to 4 min for music.
- **Shape:** 9:16, 2:3, 3:4, 4:5, 1:1, 4:3, 3:2, 16:9 and 21:9.
- **Look number:** with *Keep this look* on, type a number to repeat or share a look.

## How aivo gets better results

Open models can come close to hosted ones like Gemini, but only with the right settings and a
prompt in the form they were trained on. aivo does four things:

1. **Improve my description** (on by default; per creation). Short descriptions make flat pictures:
   the engines were trained on long, specific captions. Before creating, the worker runs a small
   language model on the same GPU (Qwen3-4B, inside ComfyUI) that rewrites the description. It
   adds light, setting, composition and camera, and for videos the motion in order and the sound.
   - The instructions are in `catalog/rewriting.yaml`, one set per kind of creation. Manifests pick
     one per mode (`rewrite: {t2v: video, i2v: animate}`).
   - aivo checks the result: empty, cut-off or refusing text is dropped, and adult creations get
     the minors check again. When the result can't be used, the creation is made from the
     description as written; nothing fails because of it.
   - The improved text is shown on the creation's page (*Improved description*). It is sealed in
     the database like the description itself.
   - Edits and music use the words as written: an edit instruction isn't a picture description.
   - Qwen3-4B is the same file Z-Image and FLUX.2 [klein] use as their text encoder (8 GB). Other
     engines download it once per worker, or once to the network volume.
2. **Stronger engines and settings.**
   - Pictures at Standard and Best go to FLUX.2 [dev].
   - Wan 2.2 videos have four quality levels. A 4-step speed-up makes it quick, but its motion can be
     timid; the full model moves better but takes about 9× as long. **High** is the middle way.

     | Level | Wan 2.2 | Time for 5 s (RTX PRO 6000) |
     |---|---|---|
     | Draft | 4-step speed-up, 480p | < 1 min |
     | Standard | 4-step speed-up, 720p | ~1.5 min |
     | High | the full model guides the first 2 of 8 steps (motion, layout), the speed-up does the rest | ~3–4 min |
     | Best | the full model, 20 steps (ComfyUI's template) | ~13 min |

     High is only offered where an engine has a middle way (Wan 2.2 now). Elsewhere it isn't shown,
     and an engine without it uses its Standard settings.
   - LTX-2 uses its official two-stage pipeline: 8 steps at half size, Lightricks' 2× latent
     upscaler, then 3 refining steps at full size.
3. **Finishing.** Wan 2.2 makes 16 frames per second, which stutters. ComfyUI's built-in frame
   interpolation (FILM) doubles that to 32 fps after creating.
4. **Two pictures per creation** by default: one is usually clearly better.

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
  rewording a refused description is easy, a wrong creation can't be undone. An improved
  description is checked again; if it fails, the creation uses the words as written.
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
   - **Quality presets**: map Draft/Standard/Best to concrete parameters and sizes. *High* is optional:
     a middle way between Standard and Best, offered only where an engine has one.
   - **Benchmark seconds** per GPU class: priors that measured timings replace over time.
   - **Sizes** for the shapes the engine was tuned for. Other shapes (there are nine, 9:16 to 21:9) are
     worked out from the preset's pixel budget, in multiples of `size_multiple` (default 16).
   - **How many at once:** `max_batch` is what one run makes (GPU memory). A creation may have more,
     up to 8 pictures or 4 videos or songs (`max_count` can lower that), made one batch after another.
   - **Length:** videos and music take any length from `min_seconds` to `max_seconds`.
   - **License** and a user-facing **tagline**.
2. Export the ComfyUI workflow in **API format** to `workflows/<mode>.json`. Replace the
   values you want to control with placeholders such as `"$prompt"`, `"$seed"`, `"$width"`,
   `"$height"`, `"$steps"`, `"$frames"`, `"$fps"`, `"$batch"`, `"$prefix"` and
   `"$reference_image"`. Any key from the quality preset or variant `params` also works.
   Video manifests can set `video.interpolate: 2` and use `"$out_fps"` and `"$interpolate"` for a
   frame interpolation step.
3. Optionally add it to `catalog/routing.yaml` so *Auto* can pick it, and pick rewriting
   instructions per mode with `rewrite:` (see `catalog/rewriting.yaml`).
4. Run `uv run aivo catalog-check` and `uv run pytest backend/tests/unit/test_catalog.py`.
   Every combination must render.
5. Try it once on RunPod with a low spending limit (Settings → Spending limits).

No worker rebuild is needed. The backend renders workflows; the worker fetches and runs them.
