"""Build scorer inputs from the catalog."""

from __future__ import annotations

from aivo.catalog.presets import Plan
from aivo.placement.scorer import Request, VariantNeed


def build_request(plan: Plan, preference: str, *, k: int = 1, min_vram_override: float | None = None) -> Request:
    mf = plan.model.manifest
    qf = mf.bench.quality_factor.get(plan.quality, 1.0)
    variants = tuple(
        VariantNeed(
            variant_id=v.id,
            precision=v.precision,
            min_vram_gb=v.min_vram_gb,
            recommended_vram_gb=v.recommended_vram_gb,
            min_ram_gb=v.min_ram_gb,
            vendors=tuple(v.vendors),
            quality_rank=v.quality_rank,
            weight_bytes=mf.weight_bytes(v, plan.mode),
            file_dests=frozenset(f.dest for f in mf.files_for(v, plan.mode)),
        )
        for v in mf.variants
    )
    return Request(
        model_id=mf.id,
        quality=plan.quality,
        variants=variants,
        bench_s_per_unit={c: s * qf for c, s in mf.bench.seconds.items()},
        model_init_s=mf.bench.model_init_s,
        units=plan.units,
        output_mb=mf.output_mb * plan.output.count,
        preference=preference,
        k=k,
        min_vram_override=min_vram_override,
    )
