"""The description rewriter's backend half: what the worker gets, and how the language model's text is checked."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from aivo.catalog.loader import Catalog
from aivo.catalog.presets import PROMPT_MARKER, TEXT_PARAMS, FriendlyParams, plan, workflow_params
from aivo.config import REPO_ROOT
from aivo.controller.jobspec import accept_rewrite, work_spec
from aivo.storage.keyring import Keyring


@pytest.fixture(scope="module")
def cat() -> Catalog:
    return Catalog.load(REPO_ROOT / "catalog")


def ok(cat: Catalog, text: str, *, adult: bool = False) -> str | None:
    assert cat.rewriter is not None
    return accept_rewrite(cat.rewriter, text, adult=adult)[0]


LONG = "A red fox stands in deep fresh snow at dawn, its breath visible, soft pink light on the trees."


def test_text_is_tidied(cat):
    assert ok(cat, f"<think>\n\n</think>\n\n{LONG}<|im_end|>") == LONG
    assert ok(cat, f"**Prompt:** {LONG}") == LONG
    assert ok(cat, f'"{LONG}"') == LONG
    assert ok(cat, f"{LONG[:40]}\n\n{LONG[40:]}") == f"{LONG[:40]} {LONG[40:]}"


def test_unusable_text_is_refused(cat):
    assert ok(cat, "") is None
    assert ok(cat, "A fox.") is None  # too short
    assert ok(cat, "<think>the user wants a fox, so I should") is None  # ran out of tokens while thinking
    assert ok(cat, "I'm sorry, but I can't help with that request.") is None
    assert ok(cat, "I cannot create content like this, but here is an alternative") is None


def test_long_text_is_cut_at_a_sentence(cat):
    text = " ".join([LONG] * 60)
    out = ok(cat, text)
    assert out and len(out) <= cat.rewriter.max_chars and out.endswith(".")


def test_adult_creations_never_get_minors_from_the_rewriter(cat):
    teen = "A teenager walks along the beach at sunset, warm golden light, gentle waves, film grain."
    assert ok(cat, teen) == teen  # an ordinary creation may show a teenager
    assert ok(cat, teen, adult=True) is None
    assert (
        ok(cat, "A 16 year old walks along the beach at sunset, warm golden light, gentle waves.", adult=True) is None
    )


def test_every_rewriting_engine_has_instructions(cat):
    rw = cat.rewriter
    assert rw is not None and rw.files[0].dest == "text_encoders/qwen_3_4b.safetensors"
    for m in cat.models.values():
        for mode, rid in m.manifest.rewrite.items():
            assert rid in rw.instructions and mode in m.workflows
    # edits and music keep the user's words: an edit instruction isn't a picture description
    assert "edit" not in cat.get("flux2-klein-4b").manifest.rewrite
    assert not cat.get("ace-step-15").manifest.rewrite


def _job(cat: Catalog, kr: Keyring, fp: FriendlyParams, *, used: str | None = None) -> SimpleNamespace:
    pl = plan(cat, fp, set(cat.models))
    v = pl.model.manifest.variants[0]
    params = workflow_params(cat, pl, fp, v, seed=42, prefix="aivo_x")
    jid = uuid.uuid4()
    return SimpleNamespace(
        id=jid,
        model_id=pl.model.id,
        friendly_params=fp.model_dump(mode="json", exclude={"prompt", "avoid", "lyrics"}),
        prompt_enc=kr.seal(fp.prompt, jid.bytes),
        avoid_enc=kr.seal(fp.avoid, jid.bytes) if fp.avoid else None,
        lyrics_enc=None,
        prompt_used_enc=kr.seal(used, jid.bytes) if used else None,
        resolved={
            "variant_id": v.id,
            "mode": pl.mode,
            "quality": pl.quality,
            "params": {k: x for k, x in params.items() if k not in TEXT_PARAMS},
            "output": pl.output.model_dump(),
            "units": pl.units,
            "inputs": [],
            "seed": 42,
            "timeout_s": 600,
        },
    )


@pytest.fixture
def app(cat):
    kr = Keyring.parse(Keyring.generate_spec())
    return SimpleNamespace(catalog=cat, keyring=kr, env=SimpleNamespace(hf_token=None))


def test_work_spec_puts_the_language_model_first_and_marks_the_description(app, cat):
    fp = FriendlyParams(kind="video", prompt="a fox runs through snow", model_id="wan22-a14b", quality="draft")
    spec = work_spec(app, _job(cat, app.keyring, fp))
    assert spec.rewrite is not None
    assert spec.files[0].dest == "text_encoders/qwen_3_4b.safetensors"
    assert spec.rewrite.files == ["text_encoders/qwen_3_4b.safetensors"]
    assert PROMPT_MARKER in str(spec.workflow) and "a fox runs" not in str(spec.workflow)
    assert spec.rewrite.fallback == "a fox runs through snow"
    text = spec.rewrite.workflow["2"]["inputs"]["prompt"]
    assert "text-to-video" in text and text.endswith("Description: a fox runs through snow\n/no_think")
    assert spec.rewrite.workflow["2"]["inputs"]["sampling_mode.seed"] == 42


def test_zimage_shares_its_text_encoder_with_the_language_model(app, cat):
    fp = FriendlyParams(kind="image", prompt="a fox", model_id="z-image-turbo")
    spec = work_spec(app, _job(cat, app.keyring, fp))
    dests = [f.dest for f in spec.files]
    assert dests.count("text_encoders/qwen_3_4b.safetensors") == 1


def test_older_workers_get_the_description_as_written(app, cat):
    fp = FriendlyParams(kind="image", prompt="a fox", model_id="z-image-turbo", style="photo")
    spec = work_spec(app, _job(cat, app.keyring, fp), can_rewrite=False)
    assert spec.rewrite is None and PROMPT_MARKER not in str(spec.workflow)
    assert "a fox, photorealistic" in str(spec.workflow)


def test_a_retry_reuses_the_improved_description(app, cat):
    fp = FriendlyParams(kind="image", prompt="a fox", model_id="z-image-turbo")
    spec = work_spec(app, _job(cat, app.keyring, fp, used=LONG))
    assert spec.rewrite is None and LONG in str(spec.workflow)


def test_trigger_words_stay_outside_the_rewrite(app, cat):
    adult = cat.get("wan22-i2v-adult").manifest
    assert adult.prompt_prefix  # this add-on needs its trigger word in front
    fp = FriendlyParams(kind="video", prompt="she turns and smiles", model_id="wan22-i2v-adult")
    job = _job(cat, app.keyring, fp.model_copy(update={"reference_asset_id": uuid.uuid4()}))
    spec = work_spec(app, job)
    assert f"{adult.prompt_prefix}, {PROMPT_MARKER}" in str(spec.workflow)
    assert adult.prompt_prefix not in spec.rewrite.fallback


def test_text_the_user_never_wrote_is_dropped(cat):
    """Seen on RunPod: Qwen3-4B added a tagline to a poster and a chalkboard text to a cafe."""
    rw = cat.rewriter
    poster = (
        "A vibrant digital illustration of a poster for a summer music festival called 'Sunwave 2026', with a "
        "large stylized sunburst and waves. The text 'Sunwave 2026' is displayed in a bold, modern font, with the "
        "tagline 'Feel the Beat of Summer' below it. The overall composition is energetic and inviting."
    )
    out, _ = accept_rewrite(
        rw, poster, adult=False, original="a poster for a summer music festival called 'Sunwave 2026'"
    )
    assert "Feel the Beat" not in out and "'Sunwave 2026'" in out and out.endswith("energetic and inviting.")
    cafe = (
        "A cozy coffee shop on a rainy evening, warm light on wooden tables. A chalkboard near the door reads "
        '"It\'s raining outside" in faded script. Rain streams down the large window.'
    )
    out, _ = accept_rewrite(rw, cafe, adult=False, original="a cozy coffee shop on a rainy evening")
    assert "chalkboard" not in out and "Rain streams" in out
    # apostrophes aren't quotes; quoted words the user wrote stay
    cat_text = "The cat's fur is golden-brown, it's sitting on a sunny windowsill with plants around it."
    assert accept_rewrite(rw, cat_text, adult=False, original="a cat on a windowsill")[0] == cat_text
    sign = 'A shop front with a neon sign that says "OPEN LATE" glowing in the rain at night.'
    assert accept_rewrite(rw, sign, adult=False, original='a shop with a sign saying "open late"')[0] == sign
