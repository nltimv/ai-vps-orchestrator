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
Quality = Literal["draft", "standard", "best"]
Aspect = Literal["square", "portrait", "landscape", "wide"]
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

    def frames_for(self, seconds: float) -> int:
        seconds = max(0.5, min(seconds, self.max_seconds))
        raw = round(seconds * self.fps)
        n = max(1, round((raw - self.frame_offset) / self.frame_multiple))
        return n * self.frame_multiple + self.frame_offset


class MusicTiming(BaseModel):
    durations: dict[str, float] = Field(default_factory=lambda: {"30s": 30.0, "1min": 60.0, "3min": 180.0})
    max_seconds: float = 240.0


class Bench(BaseModel):
    """Seconds per unit on reference GPU classes; replaced by measured telemetry over time."""

    unit: Literal["image", "video_second", "audio_second"]
    seconds: dict[str, float]  # gpu_class -> seconds per unit at "standard" quality
    quality_factor: dict[str, float] = Field(
        default_factory=lambda: {"draft": 0.6, "standard": 1.0, "best": 1.6}
    )
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
    max_count: int = 4
    output_mb: float = 2.0  # rough per-output size estimate (for transfer time)
    tags: list[str] = Field(default_factory=list)
    enabled_by_default: bool = True
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
        for q in ("draft", "standard", "best"):
            if q not in self.quality:
                raise ValueError(f"{self.id}: missing quality preset {q!r}")
        return self

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
