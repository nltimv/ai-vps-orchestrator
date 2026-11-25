from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from aivo.catalog.loader import Catalog
from aivo.catalog.presets import FriendlyParams, plan
from aivo.config import REPO_ROOT, EnvSettings
from aivo.placement.request import build_request
from aivo.placement.scorer import Context, rank
from aivo.providers.runpod.catalog import GpuCatalog
from aivo.providers.runpod.fake import FakeRunPod

CAT = Catalog.load(REPO_ROOT / "catalog")
ENABLED = set(CAT.models)


SMALL = "NVIDIA RTX 4000 Ada Generation"
L40S = "NVIDIA L40S"


async def offers():
    return await GpuCatalog(FakeRunPod(), EnvSettings(_env_file=None)).offers()


def ctx(**kw):
    base = Context(gpu_perf={g.id: g.perf for g in CAT.gpu_classes.values()})
    for k, v in kw.items():
        setattr(base, k, v)
    return base


def req(kind="image", quality="standard", preference="balanced", model=None, duration=None, **kw):
    fp = FriendlyParams(kind=kind, prompt="a lighthouse", quality=quality, model_id=model, duration=duration)
    return build_request(plan(CAT, fp, ENABLED), preference, **kw)


async def test_fast_image_draft_prefers_small_gpu():
    r = req(quality="draft", model="z-image-turbo")
    c = rank(r, await offers(), [], ctx())
    assert c[0].offer.size_slug == SMALL
    assert "Cheapest option that fits" in c[0].reasons
    assert c[0].suitability == 100
    # 8x sizes are never candidates
    assert all(x.offer.gpu_count == 1 for x in c)


async def test_amd_excluded_for_nvidia_only_models():
    c = rank(req(model="z-image-turbo"), await offers(), [], ctx())
    assert all(x.offer.vendor == "nvidia" for x in c)


async def test_vram_floor_respected():
    c = rank(req(kind="video", model="wan22-a14b"), await offers(), [], ctx())
    assert c and all(x.offer.vram_gb >= x.variant.min_vram_gb for x in c)
    assert all(x.offer.size_slug != SMALL for x in c)


async def test_wan_balanced_picks_48gb_fast_best_picks_h100():
    o = await offers()
    # Draft (the 4-step speed-up): a short job, so a 48 GB card is the balanced pick
    bal = rank(req(kind="video", model="wan22-a14b", quality="draft"), o, [], ctx())
    assert bal[0].offer.vram_gb == 48
    # Standard runs the full model for minutes: on RunPod's prices an H100 can be worth it
    bal_std = rank(req(kind="video", model="wan22-a14b", quality="standard"), o, [], ctx())
    assert bal_std[0].offer.vram_gb >= 48
    fast = rank(req(kind="video", model="wan22-a14b", quality="best", preference="fast"), o, [], ctx())
    assert fast[0].offer.gpu_class in ("h100", "h200")
    bal_best = rank(req(kind="video", model="wan22-a14b", quality="best", preference="balanced"), o, [], ctx())
    assert bal_best[0].offer.vram_gb >= 48  # on RunPod's prices an H100 can be worth it for long videos


async def test_a_gpu_type_with_a_worker_up_starts_without_waiting():
    o = await offers()
    r = req(model="z-image-turbo")
    cold = {x.offer.size_slug: x for x in rank(r, o, [], ctx())}
    warm = {x.offer.size_slug: x for x in rank(r, o, [], ctx(warm_sizes=frozenset({L40S})))}
    assert warm[L40S].wait_s < cold[L40S].wait_s
    assert "A GPU of this type is already on — no start-up wait" in warm[L40S].reasons


async def test_cooldown_and_failures_demote():
    o = await offers()
    r = req(model="z-image-turbo", quality="draft")
    c = rank(r, o, [], ctx(cooldown=frozenset({(SMALL, "eu")})))
    assert all(x.offer.size_slug != SMALL for x in c)
    c2 = rank(r, o, [], ctx(p_fail={(SMALL, "eu"): 0.95}))
    rtx = next(x for x in c2 if x.offer.size_slug == SMALL)
    assert "Often sold out lately" in rtx.reasons


async def test_allow_new_false_only_live():
    c = rank(req(), await offers(), [], ctx(allow_new=False))
    assert c == []


async def test_regions_filter():
    c = rank(req(model="z-image-turbo"), await offers(), [], ctx(allowed_regions=frozenset({"us"})))
    assert c == []  # RunPod GPUs here are in "eu"
    c = rank(req(model="z-image-turbo"), await offers(), [], ctx(allowed_regions=frozenset({"eu"})))
    assert c and all(x.offer.region == "eu" for x in c)


async def test_oom_bump_raises_floor():
    c = rank(req(model="z-image-turbo", min_vram_override=40), await offers(), [], ctx())
    assert all(x.offer.vram_gb >= 40 for x in c)


@settings(max_examples=30, deadline=None)
@given(st.floats(min_value=0.1, max_value=20))
def test_price_increase_never_improves_score(factor):
    import asyncio

    o = asyncio.run(offers())
    r = req(model="z-image-turbo")
    c0 = ctx(max_price_hourly=1e9)
    base = {x.offer.key: x.score for x in rank(r, o, [], c0)}
    bumped = [replace(x, price_hourly=x.price_hourly * (1 + factor)) for x in o]
    after = {x.offer.key: x.score for x in rank(r, bumped, [], c0)}
    for k, s in base.items():
        assert after[k] >= s - 1e-9


async def test_economy_never_picks_more_expensive_than_fast():
    o = await offers()
    eco = rank(req(kind="video", model="wan22-a14b", quality="best", preference="economy"), o, [], ctx())
    fast = rank(req(kind="video", model="wan22-a14b", quality="best", preference="fast"), o, [], ctx())
    assert eco[0].cost_usd <= fast[0].cost_usd + 1e-9
    assert fast[0].wait_s <= eco[0].wait_s + 1e-9
