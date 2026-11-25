import uuid

import pytest

from aivo.catalog.loader import Catalog, CatalogError
from aivo.catalog.presets import FriendlyParams, placeholders, plan, render_workflow, workflow_params
from aivo.config import REPO_ROOT

CAT = Catalog.load(REPO_ROOT / "catalog")
ALL = set(CAT.models)


def test_every_model_mode_quality_variant_renders():
    for m in CAT.models.values():
        for mode, wf in m.workflows.items():
            for q in ("draft", "standard", "high", "best"):  # "high" falls back to standard where missing
                ref = uuid.uuid4() if mode in ("edit", "i2v") else None
                fp = FriendlyParams(
                    kind=m.manifest.kind, prompt="p", model_id=m.id, quality=q, mode=mode, reference_asset_id=ref
                )
                pl = plan(CAT, fp, ALL)
                for v in m.manifest.variants:
                    out = render_workflow(wf, workflow_params(CAT, pl, fp, v, seed=7))
                    assert not placeholders(out), (m.id, mode, q, v.id)
                    for node in out.values():
                        assert "class_type" in node and "inputs" in node


def test_files_referenced_by_workflows_are_in_manifest():
    for m in CAT.models.values():
        dests = {f.dest.split("/", 1)[1] for f in m.manifest.files}
        for wf in m.workflows.values():
            for node in wf.values():
                if not isinstance(node, dict):
                    continue
                for k in ("unet_name", "clip_name", "vae_name", "lora_name", "ckpt_name", "text_encoder", "model_name"):
                    v = node.get("inputs", {}).get(k)
                    if isinstance(v, str) and not v.startswith("$"):
                        assert v in dests, (m.id, v)
            for v in m.manifest.variants:
                for val in v.params.values():
                    if isinstance(val, str) and val.endswith(".safetensors"):
                        assert val in dests, (m.id, val)


def test_video_frame_rules():
    wan = CAT.get("wan22-a14b").manifest.video
    ltx = CAT.get("ltx2").manifest.video
    assert (wan.frames_for(5) - 1) % 4 == 0 and wan.frames_for(5) == 81
    assert (ltx.frames_for(5) - 1) % 8 == 0
    assert ltx.frames_for(100) <= ltx.max_seconds * ltx.fps + ltx.frame_multiple


def test_routing_and_enabled():
    m = CAT.route("image", "t2i", "draft", ALL)
    assert m.id == "z-image-turbo"
    assert CAT.route("image", "t2i", "draft", ALL - {"z-image-turbo"}).id == "flux2-klein-4b"
    with pytest.raises(CatalogError):
        CAT.route("music", "t2m", "draft", set())


def test_reference_required_for_edit():
    with pytest.raises(CatalogError):
        plan(CAT, FriendlyParams(kind="image", prompt="x", mode="edit"), ALL)


def test_distilled_models_hide_avoid_and_noncommercial_flagged():
    assert CAT.get("z-image-turbo").manifest.supports.avoid is False
    assert CAT.get("flux2-dev").manifest.license.commercial is False
    assert all(m.manifest.license.url.startswith("https://") for m in CAT.models.values())


def test_style_and_avoid_and_lyrics():
    fp = FriendlyParams(kind="image", prompt="a cat", style="anime", avoid="dogs", model_id="qwen-image-2512")
    pl = plan(CAT, fp, ALL)
    p = workflow_params(CAT, pl, fp, pl.model.manifest.variants[0], seed=1)
    assert p["prompt"].startswith("a cat, anime style") and p["negative"].startswith("dogs")
    fp = FriendlyParams(kind="music", prompt="jazz", instrumental=True, lyrics="ignored")
    pl = plan(CAT, fp, ALL)
    p = workflow_params(CAT, pl, fp, pl.model.manifest.variants[0], seed=1)
    assert p["lyrics"] == "[instrumental]" and p["tags"] == "jazz"


def test_gated_variants_are_left_out_without_a_hugging_face_token():
    from aivo.catalog.presets import FriendlyParams, plan
    from aivo.placement.request import build_request

    pl = plan(CAT, FriendlyParams(kind="image", prompt="x", model_id="flux2-dev", quality="best"), set(CAT.models))
    with_token = {v.variant_id for v in build_request(pl, "balanced", hf_token=True).variants}
    without = {v.variant_id for v in build_request(pl, "balanced", hf_token=False).variants}
    assert with_token == {"fp8", "bf16"} and without == {"fp8"}  # bf16 comes from the gated BFL repo


def test_download_errors_say_what_to_do():
    from aivo.controller.jobspec import download_error

    files = CAT.get("flux2-dev").manifest.files
    gated = "diffusion_models/flux2-dev.safetensors: hf: HTTP 401"
    assert "AIVO_HF_TOKEN" in download_error(gated, files)
    moved = "vae/flux2-vae.safetensors: hf: HTTP 401"
    assert "refused" in download_error(moved, files) and "AIVO_HF_TOKEN" not in download_error(moved, files)
    other = "vae/flux2-vae.safetensors: size mismatch"
    assert download_error(other, files).startswith("The AI model's files couldn't be downloaded")


def test_minor_words_and_ages_are_caught_without_flagging_adults():
    from aivo.safety import minors_mentioned

    for text in (
        "a teenager",
        "Teen girl",
        "a 17 year old",
        "17yo",
        "aged 15",
        "schoolgirl uniform",
        "little girl",
        "een meisje",
        "minderjarige",
        "loli",
        "a 16-year-old",
    ):
        assert minors_mentioned(text), text
    for text in (
        "a kind woman in her thirties",
        "a 25 year old woman",
        "18 years old",
        "babydoll lingerie",
        "a teacher in a classroom",
        "adult couple",
        "steenbok in the desert",
        "kitten",
    ):
        assert minors_mentioned(text) is None, text


def test_adult_engines_are_never_picked_automatically():
    for kind, modes in CAT.routing.items():
        for qualities in modes.values():
            for ids in qualities.values():
                assert not any(CAT.get(i).manifest.adult for i in ids), (kind, ids)


def test_ltx2_runs_the_two_stage_pipeline_at_sizes_its_first_stage_can_halve():
    m = CAT.get("ltx2")
    for q in m.manifest.quality.values():
        for w, h in q.sizes.values():
            assert w % 64 == 0 and h % 64 == 0, (w, h)
    for mode in ("t2v", "i2v"):
        wf = m.workflows[mode]
        kinds = [n["class_type"] for k, n in wf.items() if not k.startswith("_")]
        assert kinds.count("SamplerCustomAdvanced") == 2 and "LTXVLatentUpsampler" in kinds
    up = {f.dest for f in m.manifest.files_for(m.manifest.variants[0], "t2v")}
    assert "latent_upscale_models/ltx-2-spatial-upscaler-x2-1.0.safetensors" in up


def test_wan_videos_are_interpolated_to_32_fps():
    for mid in ("wan22-a14b", "wan22-i2v-adult"):
        m = CAT.get(mid)
        mode = "t2v" if "t2v" in m.workflows else "i2v"
        fp = FriendlyParams(kind="video", prompt="p", model_id=mid, mode=mode, reference_asset_id=uuid.uuid4())
        pl = plan(CAT, fp, ALL)
        assert pl.output.fps == 32 and abs(pl.output.duration_s - 81 / 16) < 1e-6
        out = render_workflow(m.workflows[mode], workflow_params(CAT, pl, fp, m.manifest.variants[0], seed=1))
        interp = next(n for n in out.values() if n["class_type"] == "FrameInterpolate")
        video = next(n for n in out.values() if n["class_type"] == "CreateVideo")
        assert interp["inputs"]["multiplier"] == 2 and video["inputs"]["fps"] == 32


def _wan_passes(quality: str, model_id: str = "wan22-a14b", mode: str = "t2v") -> list[tuple]:
    """(model chain, start, end, total, cfg, speed-up strength) for each sampler pass, in order."""
    m = CAT.get(model_id)
    fp = FriendlyParams(
        kind="video",
        prompt="p",
        model_id=model_id,
        mode=mode,
        quality=quality,
        reference_asset_id=uuid.uuid4() if mode == "i2v" else None,
    )
    pl = plan(CAT, fp, ALL)
    wf = render_workflow(m.workflows[mode], workflow_params(CAT, pl, fp, m.manifest.variants[0], seed=1))

    def lora(node: str) -> float:  # the speed-up strength on a model chain
        while wf[node]["class_type"] != "UNETLoader":
            n = wf[node]
            if n["class_type"] == "LoraLoaderModelOnly" and "lightx2v" in n["inputs"]["lora_name"]:
                return n["inputs"]["strength_model"]
            node = n["inputs"]["model"][0]
        return 0.0

    passes = sorted(
        (n["inputs"] for n in wf.values() if n.get("class_type") == "KSamplerAdvanced"),
        key=lambda i: i["start_at_step"],
    )
    assert len(passes) == 3
    return [
        (i["model"][0], i["start_at_step"], i["end_at_step"], i["steps"], i["cfg"], lora(i["model"][0])) for i in passes
    ]


def test_wan_quality_ladder_from_speed_up_to_full_model():
    q = CAT.get("wan22-a14b").manifest.quality
    assert list(q) == ["draft", "standard", "high", "best"]
    for level in ("draft", "standard"):  # the 4-step speed-up everywhere, no guidance
        assert [(s, e, t, c, sp) for _, s, e, t, c, sp in _wan_passes(level)] == [
            (0, 1, 4, 1.0, 1.0),
            (1, 2, 4, 1.0, 1.0),
            (2, 10000, 4, 1.0, 1.0),
        ]
    # High: the full model guides the first 2 of 8 steps, then the speed-up
    assert [(s, e, t, c, sp) for _, s, e, t, c, sp in _wan_passes("high")] == [
        (0, 2, 8, 3.5, 0.0),
        (2, 4, 8, 1.0, 1.0),
        (4, 10000, 8, 1.0, 1.0),
    ]
    # Best: the full model all the way, as in ComfyUI's template (20 steps, the experts switch at 10)
    assert [(s, e, t, c, sp) for _, s, e, t, c, sp in _wan_passes("best")] == [
        (0, 5, 20, 3.5, 0.0),
        (5, 10, 20, 3.5, 0.0),
        (10, 10000, 20, 3.5, 0.0),
    ]
    # the same ladder for animating a picture, and for the 18+ engine
    assert _wan_passes("high", mode="i2v")[0][4:] == (3.5, 0.0)
    assert _wan_passes("high", "wan22-i2v-adult", "i2v")[1][4:] == (1.0, 1.0)


def test_high_quality_falls_back_where_an_engine_has_no_middle_way():
    assert CAT.get("ltx2").manifest.level("high") == "standard"
    assert CAT.get("wan22-a14b").manifest.level("high") == "high"
    assert CAT.route("video", "t2v", "high", ALL).id == "wan22-a14b"
    assert CAT.route("video", "i2v", "high", ALL - {"wan22-a14b"}).id == "ltx2"
    # pictures have no High: it routes and renders like Standard
    pl = plan(CAT, FriendlyParams(kind="image", prompt="p", quality="high"), ALL)
    assert pl.model.id == "flux2-dev" and pl.quality == "standard"
    pl = plan(CAT, FriendlyParams(kind="video", prompt="p", model_id="ltx2", quality="high"), ALL)
    assert pl.quality == "standard" and (pl.output.width, pl.output.height) == (1280, 704)


def test_standard_and_best_pictures_go_to_flux2_first():
    for q in ("standard", "best"):
        assert CAT.route("image", "t2i", q, ALL).id == "flux2-dev"
        assert CAT.route("image", "t2i", q, ALL - {"flux2-dev"}).id == "qwen-image-2512"
    assert CAT.route("image", "t2i", "draft", ALL).id == "z-image-turbo"


def test_every_model_file_lands_in_a_folder_comfyui_is_told_about():
    from aivo_worker.engines.comfy import MODEL_FOLDERS

    files = [f for m in CAT.models.values() for f in m.manifest.files] + list(CAT.rewriter.files)
    for f in files:
        assert f.dest.split("/", 1)[0] in MODEL_FOLDERS, f.dest


def test_every_shape_gets_a_size_the_engine_can_make():
    from aivo_protocol.catalog_types import ASPECT_RATIO

    for m in CAT.models.values():
        if m.manifest.kind == "music":
            continue
        mult = m.manifest.size_multiple
        for aspect, (rw, rh) in ASPECT_RATIO.items():
            for q in m.manifest.quality:
                mode = next(iter(m.workflows))
                fp = FriendlyParams(
                    kind=m.manifest.kind,
                    prompt="p",
                    model_id=m.id,
                    mode=mode,
                    quality=q,
                    aspect=aspect,
                    reference_asset_id=uuid.uuid4() if mode in ("edit", "i2v") else None,
                )
                o = plan(CAT, fp, ALL).output
                if aspect not in m.manifest.quality[q].sizes:  # worked out by aivo (tuned ones are the makers')
                    assert o.width % mult == 0 and o.height % mult == 0, (m.id, aspect, q, o.width, o.height)
                assert abs(o.width / o.height - rw / rh) < 0.12 * rw / rh, (m.id, aspect, q, o.width, o.height)
    # the tuned sizes stay for the original shapes; new ones keep the preset's pixel budget
    z = lambda a: plan(CAT, FriendlyParams(kind="image", prompt="p", model_id="z-image-turbo", aspect=a), ALL).output  # noqa: E731
    assert (z("square").width, z("square").height) == (1024, 1024)
    tall = z("9x16")
    assert tall.height > tall.width and 0.9 < tall.width * tall.height / 1024**2 < 1.1
    ltx = plan(CAT, FriendlyParams(kind="video", prompt="p", model_id="ltx2", aspect="21x9"), ALL).output
    assert ltx.width % 64 == 0 and ltx.height % 64 == 0


def test_any_count_up_to_the_limit_runs_in_batches():
    from aivo.catalog.presets import runs

    z = CAT.get("z-image-turbo").manifest
    assert z.count_limit == 16 and runs(z, 16) == [4, 4, 4, 4] and runs(z, 6) == [4, 2] and runs(z, 3) == [3]
    wan = CAT.get("wan22-a14b").manifest
    assert wan.count_limit == 4 and runs(wan, 3) == [1, 1, 1]
    pl = plan(CAT, FriendlyParams(kind="image", prompt="p", model_id="z-image-turbo", count=8), ALL)
    assert pl.output.count == 8 and pl.units == 8
    params = workflow_params(CAT, pl, FriendlyParams(kind="image", prompt="p", count=8), z.variants[0], seed=1)
    assert params["batch"] == 4  # the first run
    pl = plan(CAT, FriendlyParams(kind="video", prompt="p", model_id="wan22-a14b", count=4), ALL)
    assert pl.output.count == 4
    with pytest.raises(ValueError):
        FriendlyParams(kind="image", prompt="p", count=17)


def test_any_length_within_the_engines_limits():
    def secs(model, kind, d):
        return plan(CAT, FriendlyParams(kind=kind, prompt="p", model_id=model, duration=d), ALL).output.duration_s

    assert abs(secs("ltx2", "video", 7) - 7) < 0.2  # 7 s: 169 frames at 24 fps (8n+1)
    assert abs(secs("wan22-a14b", "video", 3) - 3) < 0.2
    assert secs("ltx2", "video", 60) <= 10.1 and secs("ltx2", "video", 0.2) >= 1.9  # clamped to 2..10 s
    assert secs("ace-step-15", "music", 95) == 95
    assert secs("ace-step-15", "music", 1) == 10 and secs("ace-step-15", "music", 999) == 240
    assert secs("ltx2", "video", "short") == secs("ltx2", "video", 5)  # older creations' preset keys still work
