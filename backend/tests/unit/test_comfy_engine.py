import socket
from pathlib import Path

import pytest

from aivo.catalog.loader import Catalog
from aivo.catalog.presets import FriendlyParams, plan, render_workflow, workflow_params
from aivo.config import REPO_ROOT
from aivo_protocol.worker_api import JobIn, OutputSpec
from aivo_worker.engines.base import EngineError
from aivo_worker.engines.comfy import ComfyEngine

FAKE = Path(__file__).parent.parent / "fixtures" / "fake_comfy"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
async def engine(tmp_path):
    e = ComfyEngine(FAKE, tmp_path / "models", tmp_path / "jobs" / "comfy", port=free_port())
    await e.start()
    yield e
    await e.stop()


def zimage_workflow(steps=None):
    cat = Catalog.load(REPO_ROOT / "catalog")
    fp = FriendlyParams(kind="image", prompt="a cat", model_id="z-image-turbo")
    pl = plan(cat, fp, set(cat.models))
    params = workflow_params(cat, pl, fp, pl.model.manifest.variants[0], seed=1, prefix="aivo_test")
    if steps:
        params["steps"] = steps
    return render_workflow(pl.model.workflows["t2i"], params)


async def test_engine_runs_workflow(engine, tmp_path):
    assert engine.ready and engine.gpu().vram_total_gb == 48
    seen = []
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    job = JobIn(job_id="j", model_id="z-image-turbo", workflow=zimage_workflow(), output=OutputSpec(kind="image"))
    files = await engine.run(job, tmp_path, out_dir, lambda p, s: seen.append((p, s)))
    assert len(files) == 1 and files[0].read_bytes()[:4] == b"\x89PNG"
    assert any(s == "generating" for _, s in seen)
    assert not list((engine.work_dir / "output").iterdir())  # moved out of ComfyUI's dir


async def test_engine_maps_oom(engine, tmp_path):
    job = JobIn(
        job_id="j2", model_id="z-image-turbo", workflow=zimage_workflow(steps=999), output=OutputSpec(kind="image")
    )
    with pytest.raises(EngineError) as ei:
        await engine.run(job, tmp_path, tmp_path, lambda p, s: None)
    assert ei.value.code == "oom"


async def test_extra_model_paths_written(engine):
    cfg = (engine.work_dir / "extra_model_paths.yaml").read_text()
    assert "diffusion_models: diffusion_models/" in cfg and str(engine.models_dir) in cfg


async def test_progress_never_goes_back_from_creating_to_loading(engine, tmp_path):
    """Current ComfyUI also sends progress_state/executed messages while sampling: they must not
    flip the stage back to loading_model, and progress must not go down."""
    seen: list[tuple[float, str]] = []
    out_dir = tmp_path / "out2"
    out_dir.mkdir()
    job = JobIn(job_id="j3", model_id="z-image-turbo", workflow=zimage_workflow(), output=OutputSpec(kind="image"))
    await engine.run(job, tmp_path, out_dir, lambda p, s: seen.append((p, s)))
    stages = [s for _, s in seen]
    first_gen = stages.index("generating")
    assert "loading_model" not in stages[first_gen:], stages
    values = [p for p, _ in seen]
    assert values == sorted(values), values
    assert values[-1] >= 0.9


async def test_engine_generates_text_with_the_rewriter_workflow(engine):
    """The rewriter's workflow (catalog/rewriter) runs in ComfyUI and its text comes back from PreviewAny."""
    from aivo.catalog.presets import rewrite_input

    cat = Catalog.load(REPO_ROOT / "catalog")
    rw = cat.rewriter
    assert rw is not None
    mf = cat.get("z-image-turbo").manifest
    fp = FriendlyParams(kind="image", prompt="a lighthouse at dusk", style="photo")
    wf = render_workflow(cat.rewriter_workflow, {**rw.params, "text": rewrite_input(cat, mf, "image", fp), "seed": 7})
    gen = wf["2"]["inputs"]
    assert gen["sampling_mode"] == "on" and gen["sampling_mode.seed"] == 7 and gen["max_length"] == 400
    assert gen["prompt"].endswith("Description: a lighthouse at dusk\n/no_think")
    assert "Style: Photo (photorealistic" in gen["prompt"]
    text = await engine.generate_text(wf, rw.output_node)
    assert "</think>" in text and "a lighthouse at dusk" in text
