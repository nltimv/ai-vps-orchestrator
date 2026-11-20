"""Translate friendly, jargon-free choices into concrete workflow parameters."""

from __future__ import annotations

import copy
import random
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, field_validator

from aivo.catalog.loader import Catalog, CatalogError, CatalogModel
from aivo_protocol.catalog_types import Aspect, Kind, Mode, Quality, Variant
from aivo_protocol.worker_api import OutputSpec

DEFAULT_ASPECT: dict[str, Aspect] = {"image": "square", "video": "landscape", "music": "square"}
FALLBACK_SIZES: dict[str, tuple[int, int]] = {
    "square": (1024, 1024),
    "portrait": (832, 1216),
    "landscape": (1216, 832),
    "wide": (1344, 768),
}
REFERENCE_INPUT_NAME = "reference.png"


class FriendlyParams(BaseModel):
    """What the user picks in the dashboard. No sampler/steps/CFG anywhere."""

    kind: Kind
    mode: Mode | None = None
    model_id: str | None = None  # None / "auto" = pick for me
    prompt: str = Field(min_length=1, max_length=4000)
    avoid: str | None = Field(default=None, max_length=1000)
    style: str | None = None
    aspect: Aspect | None = None
    quality: Quality = "standard"
    duration: str | None = None  # preset key, e.g. "short" or "1min"
    count: int = Field(default=1, ge=1, le=4)
    sound: bool = True
    lyrics: str | None = Field(default=None, max_length=8000)
    instrumental: bool = False
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    reference_asset_id: uuid.UUID | None = None

    @field_validator("model_id")
    @classmethod
    def _auto(cls, v: str | None) -> str | None:
        return None if v in (None, "", "auto") else v

    def resolved_mode(self) -> Mode:
        if self.mode:
            return self.mode
        has_ref = self.reference_asset_id is not None
        return {
            "image": "edit" if has_ref else "t2i",
            "video": "i2v" if has_ref else "t2v",
            "music": "t2m",
        }[self.kind]  # type: ignore[return-value]


@dataclass(frozen=True)
class Plan:
    """A model chosen for a request, before a machine (and thus variant) is picked."""

    model: CatalogModel
    mode: Mode
    quality: Quality
    units: float  # images / video seconds / audio seconds, for runtime estimates
    output: OutputSpec


def choose_model(catalog: Catalog, p: FriendlyParams, enabled: set[str]) -> CatalogModel:
    mode = p.resolved_mode()
    if p.model_id:
        m = catalog.get(p.model_id)
        if m.id not in enabled:
            raise CatalogError("that engine is turned off")
        if m.manifest.kind != p.kind or mode not in m.workflows:
            raise CatalogError("that engine can't do this kind of creation")
        return m
    return catalog.route(p.kind, mode, p.quality, enabled)


def plan(catalog: Catalog, p: FriendlyParams, enabled: set[str]) -> Plan:
    m = choose_model(catalog, p, enabled)
    mf = m.manifest
    mode = p.resolved_mode()
    if catalog.mode_needs_reference(mode) and p.reference_asset_id is None:
        raise CatalogError("this needs a starting image")
    count = min(p.count, mf.max_count)
    width, height = _size(m, p)
    if mf.kind == "image":
        units = float(count)
        out = OutputSpec(kind="image", count=count, width=width, height=height)
    elif mf.kind == "video":
        assert mf.video is not None
        secs = _duration(mf.video.durations, p.duration, "short")
        frames = mf.video.frames_for(secs)
        secs = frames / mf.video.fps
        units = secs * count
        out = OutputSpec(kind="video", count=count, width=width, height=height, duration_s=secs,
                         fps=mf.video.fps, with_audio=mf.supports.sound and p.sound)
    else:
        assert mf.music is not None
        secs = min(_duration(mf.music.durations, p.duration, "30s"), mf.music.max_seconds)
        units = secs * count
        out = OutputSpec(kind="audio", count=count, duration_s=secs)
    return Plan(model=m, mode=mode, quality=p.quality, units=units, output=out)


def _duration(table: dict[str, float], key: str | None, default_key: str) -> float:
    if key and key in table:
        return table[key]
    if key:
        try:
            return float(key)
        except ValueError:
            pass
    if default_key in table:
        return table[default_key]
    return next(iter(table.values()))


def _size(m: CatalogModel, p: FriendlyParams) -> tuple[int, int]:
    aspect = p.aspect or DEFAULT_ASPECT[m.manifest.kind]
    q = m.manifest.quality[p.quality]
    if aspect in q.sizes:
        return tuple(q.sizes[aspect])  # type: ignore[return-value]
    std = m.manifest.quality["standard"]
    if aspect in std.sizes:
        return tuple(std.sizes[aspect])  # type: ignore[return-value]
    return FALLBACK_SIZES[aspect]


def workflow_params(
    catalog: Catalog, pl: Plan, p: FriendlyParams, variant: Variant, *, seed: int | None = None, prefix: str = "aivo"
) -> dict[str, Any]:
    """All ``$placeholders`` a workflow template may use."""
    mf = pl.model.manifest
    q = mf.quality[pl.quality]
    prompt = p.prompt.strip()
    if p.style and p.style in catalog.styles:
        suffix = catalog.styles[p.style].suffix_for(mf.family)
        if suffix:
            prompt = f"{prompt}, {suffix}"
    negative = mf.defaults.get("negative", "")
    if mf.supports.avoid and p.avoid:
        negative = f"{p.avoid}, {negative}".strip(", ")
    seed = p.seed if (p.seed is not None and mf.supports.seed) else (seed if seed is not None else random.randrange(2**32))
    params: dict[str, Any] = {
        **{k: v for k, v in mf.defaults.items() if k != "negative"},
        **q.params,
        **variant.params,
        "prompt": prompt,
        "negative": negative,
        "seed": seed,
        "batch": pl.output.count,
        "prefix": prefix,
    }
    if pl.output.width:
        params["width"], params["height"] = pl.output.width, pl.output.height
    if mf.kind == "video":
        assert mf.video is not None
        params["frames"] = mf.video.frames_for(pl.output.duration_s or 5)
        params["fps"] = mf.video.fps
        params["duration"] = pl.output.duration_s
        params["sound"] = pl.output.with_audio
    if mf.kind == "music":
        params["duration"] = pl.output.duration_s
        params["tags"] = prompt
        lyrics = (p.lyrics or "").strip()
        params["lyrics"] = "[instrumental]" if (p.instrumental or not lyrics) else lyrics
    if catalog.mode_needs_reference(pl.mode):
        params["reference_image"] = REFERENCE_INPUT_NAME
    return params


class RenderError(Exception):
    pass


def render_workflow(template: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Replace every string value that is exactly ``"$name"`` with ``params[name]``.

    Keys starting with ``_`` at the top level (comments, metadata) are dropped.
    """
    used: set[str] = set()

    def sub(v: Any) -> Any:
        if isinstance(v, str) and v.startswith("$") and len(v) > 1 and v[1:].isidentifier():
            name = v[1:]
            if name not in params:
                raise RenderError(f"workflow placeholder ${name} has no value")
            used.add(name)
            return params[name]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v

    out = sub({k: copy.deepcopy(v) for k, v in template.items() if not k.startswith("_")})
    return out


def placeholders(template: Any) -> set[str]:
    found: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, str) and v.startswith("$") and len(v) > 1 and v[1:].isidentifier():
            found.add(v[1:])
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(template)
    return found

