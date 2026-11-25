"""Translate friendly, jargon-free choices into concrete workflow parameters."""

from __future__ import annotations

import copy
import math
import random
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, field_validator

from aivo.catalog.loader import Catalog, CatalogError, CatalogModel
from aivo_protocol.catalog_types import ASPECT_RATIO, MAX_COUNT, Aspect, Kind, Mode, ModelManifest, Quality, Variant
from aivo_protocol.worker_api import OutputSpec

DEFAULT_ASPECT: dict[str, Aspect] = {"image": "square", "video": "wide", "music": "square"}
FALLBACK_SIZES: dict[str, tuple[int, int]] = {
    "square": (1024, 1024),
    "portrait": (832, 1216),
    "landscape": (1216, 832),
    "wide": (1344, 768),
}
FALLBACK_AREA = 1024 * 1024
REFERENCE_INPUT_NAME = "reference.png"
# Parameters that hold the user's words. They're left out of ``jobs.resolved`` (which isn't encrypted)
# and rebuilt from the sealed description when a worker fetches the job.
TEXT_PARAMS = ("prompt", "negative", "tags", "lyrics")
# Stands in for the description in a workflow until the worker has improved it (see ``RewriteSpec``).
PROMPT_MARKER = "@@aivo:prompt@@"


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
    duration: str | float | None = None  # seconds, or a preset key such as "short" or "1min"
    count: int = Field(default=1, ge=1, le=max(MAX_COUNT.values()))
    sound: bool = True
    lyrics: str | None = Field(default=None, max_length=8000)
    instrumental: bool = False
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    reference_asset_id: uuid.UUID | None = None
    enhance: bool = True  # "Improve my description": a language model on the GPU details it first

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
    count = min(p.count, mf.count_limit)
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
        out = OutputSpec(
            kind="video",
            count=count,
            width=width,
            height=height,
            duration_s=secs,
            fps=mf.video.out_fps,
            with_audio=mf.supports.sound and p.sound,
        )
    else:
        assert mf.music is not None
        secs = mf.music.clamp(_duration(mf.music.durations, p.duration, "30s"))
        units = secs * count
        out = OutputSpec(kind="audio", count=count, duration_s=secs)
    return Plan(model=m, mode=mode, quality=mf.level(p.quality), units=units, output=out)


def _duration(table: dict[str, float], key: str | float | None, default_key: str) -> float:
    if isinstance(key, (int, float)):
        return float(key)
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
    """Width and height: the preset's tuned size for this shape, else worked out from its pixel budget."""
    mf = m.manifest
    aspect = p.aspect or DEFAULT_ASPECT[mf.kind]
    q = mf.quality[mf.level(p.quality)]
    if aspect in q.sizes:
        return tuple(q.sizes[aspect])  # type: ignore[return-value]
    sizes = q.sizes or mf.quality["standard"].sizes
    if not sizes and aspect in FALLBACK_SIZES:
        return FALLBACK_SIZES[aspect]
    area = (sum(w * h for w, h in sizes.values()) / len(sizes)) if sizes else FALLBACK_AREA
    rw, rh = ASPECT_RATIO[aspect]
    mult = mf.size_multiple
    w = max(mult, round(math.sqrt(area * rw / rh) / mult) * mult)
    h = max(mult, round(math.sqrt(area * rh / rw) / mult) * mult)
    return w, h


def runs(mf: ModelManifest, count: int) -> list[int]:
    """How many each run of the workflow makes: batches of at most ``max_batch``, one after another."""
    full, rest = divmod(count, mf.max_batch)
    return [mf.max_batch] * full + ([rest] if rest else [])


def describe(catalog: Catalog, mf: ModelManifest, p: FriendlyParams) -> str:
    """The description as an engine gets it without rewriting: the user's words and the style's phrasing."""
    prompt = p.prompt.strip()
    if p.style and p.style in catalog.styles:
        suffix = catalog.styles[p.style].suffix_for(mf.family)
        if suffix:
            prompt = f"{prompt}, {suffix}"
    return prompt


def with_prefix(mf: ModelManifest, prompt: str) -> str:
    """An add-on's trigger word goes in front, outside anything the rewriter writes."""
    return f"{mf.prompt_prefix}, {prompt}" if mf.prompt_prefix else prompt


def text_params(catalog: Catalog, mf: ModelManifest, p: FriendlyParams, *, prompt: str | None = None) -> dict[str, Any]:
    """The workflow parameters that hold the user's words (``TEXT_PARAMS``).

    ``prompt`` replaces the description (an improved one, or ``PROMPT_MARKER``).
    """
    full = with_prefix(mf, describe(catalog, mf, p) if prompt is None else prompt)
    negative = mf.defaults.get("negative", "")
    if mf.supports.avoid and p.avoid:
        negative = f"{p.avoid}, {negative}".strip(", ")
    out: dict[str, Any] = {"prompt": full, "negative": negative}
    if mf.kind == "music":
        out["tags"] = full
        lyrics = (p.lyrics or "").strip()
        out["lyrics"] = "[instrumental]" if (p.instrumental or not lyrics) else lyrics
    return out


def rewrite_for(catalog: Catalog, mf: ModelManifest, mode: str, p: FriendlyParams) -> str | None:
    """Which rewriting instructions this creation gets, if its description is to be improved."""
    if not p.enhance or catalog.rewriter is None:
        return None
    rid = mf.rewrite.get(mode)  # type: ignore[call-overload]
    return rid if rid in catalog.rewriter.instructions else None


def rewrite_input(catalog: Catalog, mf: ModelManifest, rid: str, p: FriendlyParams) -> str:
    """What the language model reads: its instructions, the requested style and the user's description."""
    rw = catalog.rewriter
    assert rw is not None
    style = ""
    if p.style and p.style in catalog.styles:
        st = catalog.styles[p.style]
        suffix = st.suffix_for(mf.family)
        style = f"Style: {st.label}" + (f" ({suffix})" if suffix else "") + "\n"
    return rw.template.format(instructions=rw.instructions[rid].strip(), style=style, description=p.prompt.strip())


def workflow_params(
    catalog: Catalog, pl: Plan, p: FriendlyParams, variant: Variant, *, seed: int | None = None, prefix: str = "aivo"
) -> dict[str, Any]:
    """All ``$placeholders`` a workflow template may use."""
    mf = pl.model.manifest
    q = mf.quality[pl.quality]
    seed = (
        p.seed if (p.seed is not None and mf.supports.seed) else (seed if seed is not None else random.randrange(2**32))
    )
    params: dict[str, Any] = {
        **{k: v for k, v in mf.defaults.items() if k != "negative"},
        **q.params,
        **variant.params,
        **text_params(catalog, mf, p),
        "seed": seed,
        "batch": min(pl.output.count, mf.max_batch),  # the first run (see runs())
        "prefix": prefix,
    }
    if pl.output.width:
        params["width"], params["height"] = pl.output.width, pl.output.height
    if mf.kind == "video":
        assert mf.video is not None
        params["frames"] = mf.video.frames_for(pl.output.duration_s or 5)
        params["fps"] = mf.video.fps
        params["out_fps"] = mf.video.out_fps
        params["interpolate"] = mf.video.interpolate
        params["duration"] = pl.output.duration_s
        params["sound"] = pl.output.with_audio
    if mf.kind == "music":
        params["duration"] = pl.output.duration_s
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
