"""Suitability scoring: which machine (size x region, or an already-running one) should run a job?

One objective, in US dollars (lower is better)::

    S = C + lambda * W/3600 + P_rel + P_over + P_quality - P_pref

* ``C``: expected cost of the job on that machine (start-up overhead, billing
  minimum and idle tail included; amortized over compatible queued jobs).
  For an already-running machine it is the marginal cost only.
* ``W``: expected wait until the result is in the gallery.
* ``lambda``: what an hour of waiting is worth to the user (Economy $1,
  Balanced $4, Fast $20). This is the single knob that trades speed for money,
  so a too-small GPU (slow) and a too-big GPU (expensive) both lose naturally.
* ``P_rel``: expected cost of a failed create (sold out) given recent history.
* ``P_over``: extra penalty for grossly oversized GPUs.
* ``P_quality``: small nudge towards higher-precision variants.
* ``P_pref``: small bonus for regions the admin prefers.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from aivo.placement.estimates import Estimator
from aivo.providers.base import GpuOffer

LAMBDA = {"economy": 1.0, "balanced": 4.0, "fast": 20.0}
P_QUALITY = {"bf16": 0.0, "fp16": 0.0, "fp8": 0.02, "q8": 0.04, "q4": 0.10}
RETRY_COST_S = 120.0
P_FAIL_PRIOR = 0.2
OFFLOAD_SLOWDOWN = 1.5


@dataclass(frozen=True)
class VariantNeed:
    variant_id: str
    precision: str
    min_vram_gb: float
    recommended_vram_gb: float
    min_ram_gb: float
    vendors: tuple[str, ...]
    quality_rank: int
    weight_bytes: int
    file_dests: frozenset[str]


@dataclass(frozen=True)
class Request:
    model_id: str
    quality: str
    variants: tuple[VariantNeed, ...]
    bench_s_per_unit: dict[str, float]  # gpu_class -> seconds per unit (quality factor applied)
    model_init_s: float
    units: float
    output_mb: float
    preference: str = "balanced"
    k: int = 1  # compatible jobs that can share the machine (amortization)
    min_vram_override: float | None = None  # bumped after an out-of-memory failure


@dataclass(frozen=True)
class LiveInstance:
    instance_id: uuid.UUID
    offer: GpuOffer
    state: str  # ready | busy | booting | provisioning | creating
    files_present: frozenset[str] = frozenset()
    models_loaded: tuple[str, ...] = ()
    queue_s: float = 0.0  # remaining work ahead of a new job
    remaining_boot_s: float = 0.0


@dataclass
class Context:
    preference: str = "balanced"
    allowed_regions: frozenset[str] = frozenset()
    preferred_regions: frozenset[str] = frozenset()
    denied_sizes: frozenset[str] = frozenset()
    max_price_hourly: float = 10.0
    max_gpu_count: int = 1
    idle_timeout_s: float = 600
    min_billing_s: float = 300
    # RunPod Serverless: waiting in the queue is not billed (the worker's start-up is: part of t_create)
    create_unbilled: bool = False
    warm_sizes: frozenset[str] = frozenset()  # GPU types with a worker up (no cold start)
    tail_share: float = 0.5
    allow_new: bool = True  # False when max_instances reached / budget locked
    estimator: Estimator = field(default_factory=Estimator)
    gpu_perf: dict[str, float] = field(default_factory=dict)
    p_fail: dict[tuple[str, str], float] = field(default_factory=dict)
    cooldown: frozenset[tuple[str, str]] = frozenset()
    latency_ms: dict[str, float] = field(default_factory=dict)
    snapshot_regions: frozenset[str] = frozenset()
    volume_files: dict[str, frozenset[str]] = field(default_factory=dict)  # region -> dests on cache volume
    mirror_available: bool = False
    bandwidth_mbps: float = 160.0  # backend <-> machine, for output transfer


@dataclass
class Candidate:
    offer: GpuOffer
    variant: VariantNeed
    instance_id: uuid.UUID | None
    wait_s: float
    cost_usd: float
    score: float
    p_fail: float
    times: dict[str, float]
    components: dict[str, float]
    flags: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)
    suitability: int = 0

    @property
    def key(self) -> str:
        return f"instance:{self.instance_id}" if self.instance_id else self.offer.key


def _run_seconds(req: Request, ctx: Context, gpu_class: str | None) -> float:
    b = req.bench_s_per_unit
    per_unit: float | None = b.get(gpu_class) if gpu_class else None
    if per_unit is None:
        # scale from the closest known class by relative performance
        perf = ctx.gpu_perf.get(gpu_class or "", 1.0)
        refs = [(c, s) for c, s in b.items() if c in ctx.gpu_perf]
        if refs:
            c, s = max(refs, key=lambda cs: ctx.gpu_perf[cs[0]])
            per_unit = s * ctx.gpu_perf[c] / max(perf, 0.1)
        else:
            per_unit = 30.0
    per_unit = ctx.estimator.run_seconds_per_unit(req.model_id, req.quality, gpu_class, per_unit)
    return per_unit * req.units


def fitting_variants(req: Request, offer: GpuOffer) -> list[VariantNeed]:
    min_vram = req.min_vram_override or 0.0
    return [
        v
        for v in req.variants
        if offer.vendor in v.vendors
        and offer.vram_gb >= max(v.min_vram_gb, min_vram)
        and offer.ram_gb >= v.min_ram_gb
        and offer.disk_gb + offer.scratch_gb >= v.weight_bytes / 1e9 * 1.1 + 40
    ]


QUALITY_WEIGHT = {"draft": 0.5, "standard": 1.0, "high": 2.5, "best": 5.0}


def _load_seconds(
    req: Request,
    ctx: Context,
    v: VariantNeed,
    region: str,
    present: frozenset[str],
    loaded: bool,
    gpu_class: str | None,
) -> tuple[float, str]:
    missing = v.file_dests - present
    if not missing and loaded:
        return 0.0, "loaded"
    init = ctx.estimator.model_init(req.model_id, gpu_class, req.model_init_s)
    if not missing:
        return init, "local"
    frac = len(missing) / max(1, len(v.file_dests))
    gb = v.weight_bytes / 1e9 * frac
    vol = ctx.volume_files.get(region, frozenset())
    if missing <= vol:
        source = "volume"
    elif ctx.mirror_available:
        source = "bucket"
    else:
        source = "hf"
    return gb / ctx.estimator.throughput_gbps(source, region) + init, source


def rank(req: Request, offers: Iterable[GpuOffer], live: Iterable[LiveInstance], ctx: Context) -> list[Candidate]:
    lam = LAMBDA.get(req.preference or ctx.preference, LAMBDA["balanced"])
    cands: list[Candidate] = []

    def common(offer: GpuOffer) -> bool:
        if ctx.allowed_regions and offer.region not in ctx.allowed_regions:
            return False
        return offer.gpu_count <= ctx.max_gpu_count and offer.size_slug not in ctx.denied_sizes

    # -- already running / starting machines (marginal cost) -------------------------
    for inst in live:
        if not common(inst.offer):
            continue
        best_c: Candidate | None = None
        for v in fitting_variants(req, inst.offer):
            c = _eval_live(req, ctx, inst, v, lam)
            if best_c is None or c.score < best_c.score:
                best_c = c
        if best_c is not None:
            cands.append(best_c)

    # -- new machines ---------------------------------------------------------------------
    if ctx.allow_new:
        for offer in offers:
            if not common(offer) or not offer.available:
                continue
            if offer.price_hourly > ctx.max_price_hourly:
                continue
            if (offer.size_slug, offer.region) in ctx.cooldown:
                continue
            best_c = None
            for v in fitting_variants(req, offer):
                c = _eval_new(req, ctx, offer, v, lam)
                if best_c is None or c.score < best_c.score:
                    best_c = c
            if best_c is not None:
                cands.append(best_c)

    cands.sort(key=lambda c: (c.score, c.wait_s))
    _explain(cands, ctx)
    return cands


def _eval_live(req: Request, ctx: Context, inst: LiveInstance, v: VariantNeed, lam: float) -> Candidate:
    t_load, source = _load_seconds(
        req, ctx, v, inst.offer.region, inst.files_present, req.model_id in inst.models_loaded, inst.offer.gpu_class
    )
    t_run = _run_seconds(req, ctx, inst.offer.gpu_class)
    if inst.offer.vram_gb < v.recommended_vram_gb:
        t_run *= OFFLOAD_SLOWDOWN
    t_xfer = _xfer(req, ctx, inst.offer.region)
    wait = inst.remaining_boot_s + inst.queue_s + t_load + t_run + t_xfer
    cost = inst.offer.price_hourly / 3600 * (t_load + t_run)
    comps = {
        "cost": cost,
        "wait": lam * wait / 3600,
        "rel": 0.0,
        "over": _over(cost, inst.offer, v),
        "quality": P_QUALITY.get(v.precision, 0.05) * QUALITY_WEIGHT.get(req.quality, 1.0),
        "pref": -0.02 if inst.offer.region in ctx.preferred_regions else 0.0,
    }
    flags = {"running" if inst.state in ("ready", "busy") else "starting"}
    if source in ("loaded", "local"):
        flags.add("model_ready")
    return _cand(
        inst.offer,
        v,
        inst.instance_id,
        wait,
        cost,
        comps,
        0.0,
        flags,
        {
            "boot": inst.remaining_boot_s,
            "queue": inst.queue_s,
            "load": t_load,
            "run": t_run,
            "xfer": t_xfer,
        },
        source,
    )


def _eval_new(req: Request, ctx: Context, offer: GpuOffer, v: VariantNeed, lam: float) -> Candidate:
    image_kind = "snapshot" if offer.region in ctx.snapshot_regions else "base"
    warm = offer.size_slug in ctx.warm_sizes
    # RunPod: t_create = from submitting until a worker takes the job (queue + the worker's cold start)
    t_create = 5.0 if warm else ctx.estimator.t_create(offer.size_slug, offer.region, image_kind)
    t_boot = 0.0 if warm else ctx.estimator.t_boot(offer.size_slug, offer.region, image_kind)
    t_load, source = _load_seconds(req, ctx, v, offer.region, frozenset(), False, offer.gpu_class)
    t_run = _run_seconds(req, ctx, offer.gpu_class)
    if offer.vram_gb < v.recommended_vram_gb:
        t_run *= OFFLOAD_SLOWDOWN
    t_xfer = _xfer(req, ctx, offer.region)
    fixed = t_create + t_boot + t_load
    wait = fixed + t_run + t_xfer
    k = max(1, req.k)
    # waiting in the queue is free, the worker's start is billed: count about half of t_create
    billed_fixed = fixed - t_create / 2 if ctx.create_unbilled else fixed
    billable = max(billed_fixed + k * t_run, ctx.min_billing_s) + ctx.tail_share * ctx.idle_timeout_s
    cost = offer.price_hourly / 3600 * billable / k
    p_fail = ctx.p_fail.get((offer.size_slug, offer.region), P_FAIL_PRIOR)
    if not offer.regions_known:
        p_fail = max(p_fail, 0.5)
    comps = {
        "cost": cost,
        "wait": lam * wait / 3600,
        "rel": p_fail * lam * RETRY_COST_S / 3600,
        "over": _over(cost, offer, v),
        "quality": P_QUALITY.get(v.precision, 0.05) * QUALITY_WEIGHT.get(req.quality, 1.0),
        "pref": -0.02 if offer.region in ctx.preferred_regions else 0.0,
    }
    flags = {"warm" if warm else "new"}
    if source == "volume":
        flags.add("volume")
    return _cand(
        offer,
        v,
        None,
        wait,
        cost,
        comps,
        p_fail,
        flags,
        {
            "create": t_create,
            "boot": t_boot,
            "load": t_load,
            "run": t_run,
            "xfer": t_xfer,
        },
        source,
    )


def _xfer(req: Request, ctx: Context, region: str) -> float:
    rtt = ctx.latency_ms.get(region, 80.0) / 1000
    return req.output_mb * 8 / max(ctx.bandwidth_mbps, 1) + 2 * rtt


def _over(cost: float, offer: GpuOffer, v: VariantNeed) -> float:
    ratio = offer.vram_gb / max(v.recommended_vram_gb, 1)
    return 0.15 * max(cost, 0.01) * max(0.0, math.log2(ratio) - 1)


def _cand(offer, v, iid, wait, cost, comps, p_fail, flags, times, source) -> Candidate:  # type: ignore[no-untyped-def]
    times = {**times, "source": 0.0}
    flags = set(flags)
    flags.add(f"source:{source}")
    return Candidate(
        offer=offer,
        variant=v,
        instance_id=iid,
        wait_s=wait,
        cost_usd=cost,
        score=sum(comps.values()),
        p_fail=p_fail,
        times=times,
        components=comps,
        flags=flags,
    )


def _explain(cands: list[Candidate], ctx: Context) -> None:
    if not cands:
        return
    best = cands[0].score
    cheapest = min(c.cost_usd for c in cands)
    fastest = min(c.wait_s for c in cands)
    for c in cands:
        c.suitability = max(1, round(100 * best / c.score)) if c.score > 0 else 100
        r: list[str] = []
        if "running" in c.flags or "warm" in c.flags:
            r.append("A GPU of this type is already on — no start-up wait")
        elif "starting" in c.flags:
            r.append("Already starting up")
        if c.cost_usd <= cheapest * 1.001:
            r.append("Cheapest option that fits")
        if c.wait_s <= fastest * 1.001:
            r.append("Fastest result")
        if "model_ready" in c.flags:
            r.append("AI model already loaded")
        elif "volume" in c.flags:
            r.append("AI model cached in this region")
        if c.p_fail >= 0.4:
            r.append("Often sold out lately")
        if c.offer.vram_gb >= 2 * c.variant.recommended_vram_gb:
            r.append("More power than this needs")
        elif c.offer.vram_gb < c.variant.recommended_vram_gb:
            r.append("Tight fit — a bit slower")
        if ctx.latency_ms.get(c.offer.region, 0) > 150:
            r.append("Far away — results take longer to arrive")
        if c.offer.region in ctx.preferred_regions:
            r.append("In your preferred region")
        c.reasons = r or ["Good balance of speed and price"]
