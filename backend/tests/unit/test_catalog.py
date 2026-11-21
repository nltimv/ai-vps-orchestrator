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
            for q in ("draft", "standard", "best"):
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
                for k in ("unet_name", "clip_name", "vae_name", "lora_name", "ckpt_name", "text_encoder"):
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
    from aivo.controller.dispatcher import download_error
    from aivo_protocol.worker_api import FileState

    files = CAT.get("flux2-dev").manifest.files
    gated = FileState(dest="diffusion_models/flux2-dev.safetensors", state="failed", error="hf: HTTP 401")
    assert "AIVO_HF_TOKEN" in download_error([gated], files)
    moved = FileState(dest="vae/flux2-vae.safetensors", state="failed", error="hf: HTTP 401")
    assert "refused" in download_error([moved], files) and "AIVO_HF_TOKEN" not in download_error([moved], files)
    other = FileState(dest="vae/flux2-vae.safetensors", state="failed", error="size mismatch")
    assert download_error([other], files).startswith("The AI model's files couldn't be downloaded")
