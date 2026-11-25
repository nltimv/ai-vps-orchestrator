from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from aivo.api.deps import current_user, get_app
from aivo.context import AppContext
from aivo.db.models import User
from aivo.safety import adult_ok

router = APIRouter(tags=["catalog"])

KIND_LABELS = {"image": "Image", "video": "Video", "music": "Music"}
QUALITY_LABELS = {
    "draft": ("Draft", "Quick and cheap — good for trying ideas"),
    "standard": ("Standard", "Good quality for most things"),
    "high": ("High", "Between Standard and Best: livelier motion, takes about 2 to 3 times as long"),
    "best": ("Best", "Highest quality — slower and costs more"),
}
ASPECTS = [
    {"id": "square", "label": "Square", "ratio": "1:1"},
    {"id": "portrait", "label": "Portrait", "ratio": "2:3"},
    {"id": "landscape", "label": "Landscape", "ratio": "3:2"},
    {"id": "wide", "label": "Wide", "ratio": "16:9"},
]
DURATION_LABELS = {
    "short": "Short (~5 s)",
    "medium": "Longer (~10 s)",
    "30s": "30 seconds",
    "1min": "1 minute",
    "3min": "3 minutes",
}


@router.get("/catalog")
async def catalog(user: User = Depends(current_user), app: AppContext = Depends(get_app)) -> dict[str, Any]:
    rs = await app.runtime()
    enabled = app.enabled_models(rs)
    models = []
    for m in app.catalog.models.values():
        mf = m.manifest
        if mf.adult and not adult_ok(user, rs):
            continue  # never shown to accounts without adult content
        durations: dict[str, float] = {}
        if mf.video:
            durations = mf.video.durations
        elif mf.music:
            durations = mf.music.durations
        models.append(
            {
                "id": mf.id,
                "kind": mf.kind,
                "name": mf.display_name,
                "tagline": mf.tagline,
                "description": mf.description,
                "modes": [x.mode for x in mf.modes],
                "enabled": mf.id in enabled,
                "license": mf.license.model_dump(),
                "supports": mf.supports.model_dump(),
                # modes in which "Improve my description" applies
                "improves": [m for m in mf.rewrite if app.catalog.rewriter is not None],
                "durations": [
                    {"id": k, "label": DURATION_LABELS.get(k, f"{v:.0f} s"), "seconds": v} for k, v in durations.items()
                ],
                "qualities": [q for q in QUALITY_LABELS if q in mf.quality],
                "max_count": mf.max_count,
                "min_vram_gb": min(v.min_vram_gb for v in mf.variants),
                "tags": mf.tags,
                "adult": mf.adult,
            }
        )
    return {
        "kinds": [{"id": k, "label": v, "modes": app.catalog.modes_for_kind(k)} for k, v in KIND_LABELS.items()],
        "models": models,
        "styles": [{"id": s.id, "label": s.label} for s in app.catalog.styles.values()],
        "aspects": ASPECTS,
        # which kinds offer each level: "high" only where an engine has a middle way (Wan 2.2 videos)
        "qualities": [
            {
                "id": k,
                "label": v[0],
                "hint": v[1],
                "kinds": [
                    kd
                    for kd in KIND_LABELS
                    if any(m["kind"] == kd and m["enabled"] and k in m["qualities"] for m in models)
                ],
            }
            for k, v in QUALITY_LABELS.items()
        ],
        "preferences": [
            {"id": "economy", "label": "Save money", "hint": "Cheapest machine, may take longer"},
            {"id": "balanced", "label": "Balanced", "hint": "Good mix of speed and price"},
            {"id": "fast", "label": "Fastest", "hint": "Quickest result, costs more"},
        ],
        "default_preference": rs.preference_default,
    }
