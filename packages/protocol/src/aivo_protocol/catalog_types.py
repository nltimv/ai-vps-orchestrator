"""Model catalog schema.

Each generation pipeline is described declaratively by a manifest
(``catalog/models/<id>/manifest.yaml``) plus ComfyUI API-format workflow templates.
Adding a model is data, not code.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Kind = Literal["image", "video", "music"]
Mode = Literal["t2i", "edit", "t2v", "i2v", "t2m"]
Precision = Literal["bf16", "fp16", "fp8", "q8", "q4"]
Quality = Literal["draft", "standard", "high", "best"]
# lowest to highest. Every engine has draft, standard and best; "high" is optional (a middle way where an
# engine has one): an engine without it uses its standard preset.
QUALITIES: tuple[Quality, ...] = ("draft", "standard", "high", "best")
REQUIRED_QUALITIES: tuple[Quality, ...] = ("draft", "standard", "best")
# The first four are the original shapes (manifests give tuned sizes for them); sizes for the others are
# worked out from the engine's pixel budget (see presets._size).
Aspect = Literal["square", "portrait", "landscape", "wide", "9x16", "3x4", "4x5", "4x3", "21x9"]
ASPECT_RATIO: dict[str, tuple[int, int]] = {
    "9x16": (9, 16),
    "portrait": (2, 3),
    "3x4": (3, 4),
    "4x5": (4, 5),
    "square": (1, 1),
    "4x3": (4, 3),
    "landscape": (3, 2),
    "wide": (16, 9),
    "21x9": (21, 9),
}
# Most a creation may have, per kind (an engine may allow fewer); larger counts run in several batches.
MAX_COUNT: dict[str, int] = {"image": 16, "video": 4, "music": 4}
OutputKind = Literal["image", "video", "audio"]
Vendor = Literal["nvidia", "amd"]

MODE_OUTPUT: dict[str, OutputKind] = {
    "t2i": "image",
    "edit": "image",
    "t2v": "video",
    "i2v": "video",
    "t2m": "audio",
}
MODE_NEEDS_REFERENCE = {"edit", "i2v"}


class LicenseInfo(BaseModel):
    name: str
    url: str
    commercial: bool
    summary: str = ""


class Supports(BaseModel):
    avoid: bool = False  # negative prompt ("Avoid…"); distilled models ignore it
    seed: bool = True  # "Keep this look"
    sound: bool = False  # video with generated audio
    lyrics: bool = False
    instrumental: bool = False


class FileSpec(BaseModel):
    id: str
    repo: str  # Hugging Face repo id
    revision: str = "main"  # pin to a commit SHA for production
    path: str  # path inside the repo
    dest: str  # path relative to the worker models dir (ComfyUI layout)
    bytes: int  # approximate unless sha256 is set (then exact; see `aivo catalog-pin`)
    sha256: str | None = None
    gated: bool = False  # requires accepting a license on Hugging Face (backend resolves with its token)


class Variant(BaseModel):
    id: str
    precision: Precision
    min_vram_gb: float
    recommended_vram_gb: float
    min_ram_gb: float = 16
    vendors: list[Vendor] = Field(default_factory=lambda: ["nvidia"])
    quality_rank: int = 1  # higher = better output
    files: list[str]  # needed for every mode
    mode_files: dict[str, list[str]] = Field(default_factory=dict)  # extra files per mode
    params: dict[str, Any] = Field(default_factory=dict)  # variant-specific workflow params


class ModeSpec(BaseModel):
    mode: Mode
    workflow: str  # relative to the manifest directory

    @property
    def output(self) -> OutputKind:
        return MODE_OUTPUT[self.mode]


class QualityPreset(BaseModel):
    label: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    sizes: dict[Aspect, tuple[int, int]] = Field(default_factory=dict)
    prefer_variants: list[str] = Field(default_factory=list)


class VideoTiming(BaseModel):
    fps: int
    frame_multiple: int  # Wan: 4 (4n+1), LTX: 8 (8n+1)
    frame_offset: int = 1
    durations: dict[str, float] = Field(default_factory=lambda: {"short": 5.0, "medium": 10.0})
    max_seconds: float = 10.0
    # frames added between generated ones after creating (FrameInterpolate): 2 turns 16 into 32 fps.
    # The workflow gets $fps (generating) and $out_fps (the result).
    interpolate: int = Field(default=1, ge=1, le=4)

    @property
    def out_fps(self) -> int:
        return self.fps * self.interpolate

    min_seconds: float = 2.0

    def frames_for(self, seconds: float) -> int:
        seconds = max(self.min_seconds, min(seconds, self.max_seconds))
        raw = round(seconds * self.fps)
        n = max(1, round((raw - self.frame_offset) / self.frame_multiple))
        return n * self.frame_multiple + self.frame_offset


class MusicTiming(BaseModel):
    durations: dict[str, float] = Field(default_factory=lambda: {"30s": 30.0, "1min": 60.0, "3min": 180.0})
    max_seconds: float = 240.0
    min_seconds: float = 10.0

    def clamp(self, seconds: float) -> float:
        return max(self.min_seconds, min(seconds, self.max_seconds))


class Bench(BaseModel):
    """Seconds per unit on reference GPU classes; replaced by measured telemetry over time."""

    unit: Literal["image", "video_second", "audio_second"]
    seconds: dict[str, float]  # gpu_class -> seconds per unit at "standard" quality
    quality_factor: dict[str, float] = Field(default_factory=lambda: {"draft": 0.6, "standard": 1.0, "best": 1.6})
    model_init_s: float = 20.0


class ModelManifest(BaseModel):
    id: str
    kind: Kind
    display_name: str
    tagline: str
    description: str = ""
    family: str = "generic"  # used to pick style phrasing
    license: LicenseInfo
    supports: Supports = Field(default_factory=Supports)
    modes: list[ModeSpec]
    variants: list[Variant]
    files: list[FileSpec]
    quality: dict[Quality, QualityPreset]
    video: VideoTiming | None = None
    music: MusicTiming | None = None
    bench: Bench
    defaults: dict[str, Any] = Field(default_factory=dict)
    # how many one run of the workflow makes at once (GPU memory); a creation may have more, made in
    # several runs one after another (up to max_count, default MAX_COUNT for the kind)
    max_batch: int = Field(default=4, ge=1)
    max_count: int | None = None
    size_multiple: int = 16  # width and height are multiples of this (worked-out sizes)
    output_mb: float = 2.0  # rough per-output size estimate (for transfer time)
    tags: list[str] = Field(default_factory=list)
    enabled_by_default: bool = True
    # 18+ engine: only for accounts with adult content switched on, text-made starting images only,
    # prompts involving minors refused (see aivo/safety.py)
    adult: bool = False
    prompt_prefix: str = ""  # e.g. an add-on's trigger word, put in front of every prompt
    # Improve the description on the GPU first: mode -> instructions in catalog/rewriting.yaml
    # (e.g. {t2i: image}). Modes left out (edits, music) use the description as written.
    rewrite: dict[Mode, str] = Field(default_factory=dict)
    notes: str = ""  # maintainer notes (not shown to end users)

    @model_validator(mode="after")
    def _check(self) -> ModelManifest:
        file_ids = {f.id for f in self.files}
        for v in self.variants:
            missing = (set(v.files) | {f for fs in v.mode_files.values() for f in fs}) - file_ids
            if missing:
                raise ValueError(f"{self.id}: variant {v.id} references unknown files {sorted(missing)}")
            if v.min_vram_gb > v.recommended_vram_gb:
                raise ValueError(f"{self.id}: variant {v.id} min_vram > recommended_vram")
        if not self.variants:
            raise ValueError(f"{self.id}: at least one variant required")
        if not self.modes:
            raise ValueError(f"{self.id}: at least one mode required")
        if self.kind == "video" and self.video is None:
            raise ValueError(f"{self.id}: video models need a `video` timing block")
        if self.kind == "music" and self.music is None:
            raise ValueError(f"{self.id}: music models need a `music` timing block")
        for q in REQUIRED_QUALITIES:
            if q not in self.quality:
                raise ValueError(f"{self.id}: missing quality preset {q!r}")
        for mode in self.rewrite:
            if mode not in {m.mode for m in self.modes}:
                raise ValueError(f"{self.id}: rewrite for mode {mode!r}, which the engine doesn't have")
        return self

    @property
    def count_limit(self) -> int:
        return min(self.max_count or MAX_COUNT[self.kind], MAX_COUNT[self.kind])

    def level(self, quality: str) -> Quality:
        """The preset this engine uses for ``quality``: that one, or the nearest lower one it has."""
        i = QUALITIES.index(quality) if quality in QUALITIES else QUALITIES.index("standard")
        for q in reversed(QUALITIES[: i + 1]):
            if q in self.quality:
                return q
        return "standard"

    def variant(self, variant_id: str) -> Variant:
        for v in self.variants:
            if v.id == variant_id:
                return v
        raise KeyError(variant_id)

    def mode(self, mode: str) -> ModeSpec:
        for m in self.modes:
            if m.mode == mode:
                return m
        raise KeyError(mode)

    def files_for(self, variant: Variant, mode: str | None = None) -> list[FileSpec]:
        by_id = {f.id: f for f in self.files}
        ids = list(variant.files)
        if mode is not None:
            ids += variant.mode_files.get(mode, [])
        else:
            ids += [f for fs in variant.mode_files.values() for f in fs]
        seen: set[str] = set()
        out = []
        for fid in ids:
            if fid not in seen:
                seen.add(fid)
                out.append(by_id[fid])
        return out

    def weight_bytes(self, variant: Variant, mode: str | None = None) -> int:
        return sum(f.bytes for f in self.files_for(variant, mode))

    @property
    def vendors(self) -> set[str]:
        return {vendor for v in self.variants for vendor in v.vendors}


class Rewriter(BaseModel):
    """``catalog/rewriting.yaml``: the language model that improves descriptions, and its instructions."""

    files: list[FileSpec]
    workflow: str = "rewriter/workflow.json"  # relative to the catalog root
    output_node: str
    params: dict[str, Any] = Field(default_factory=dict)  # sampling settings for the workflow
    template: str  # the model's input; {instructions}, {description} and {style} are filled in
    instructions: dict[str, str]  # id (used by manifests' `rewrite`) -> instructions
    min_chars: int = 20
    max_chars: int = 2500
    refusals: list[str] = Field(default_factory=list)  # openings that mean the model declined
