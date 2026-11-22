"""API response/request models (also the source of the frontend's TypeScript types)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from aivo.catalog.presets import FriendlyParams


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: Literal["admin", "member"]
    adult_allowed: bool = False  # the admin switched adult content on for this account
    adult_content: bool = False  # ... and adult content is switched on globally: 18+ engines are usable


class LoginIn(BaseModel):
    email: str
    password: str


class SetupIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=10, max_length=200)


class InviteIn(BaseModel):
    role: Literal["admin", "member"] = "member"
    valid_days: int = Field(default=7, ge=1, le=60)


class InviteOut(BaseModel):
    token: str
    expires_at: datetime


class AcceptInviteIn(SetupIn):
    token: str


class PlacementIn(BaseModel):
    mode: Literal["auto", "manual"] = "auto"
    offer_key: str | None = None
    allow_fallback: bool = True


class JobCreateIn(FriendlyParams):
    preference: Literal["economy", "balanced", "fast"] | None = None
    placement: PlacementIn = Field(default_factory=PlacementIn)
    confirm_over_soft_cap: bool = False


class MachineChoice(BaseModel):
    key: str
    instance_id: uuid.UUID | None
    gpu: str
    gpu_class: str | None
    vram_gb: float
    region: str
    region_name: str
    price_hourly: float
    est_wait_s: float
    est_cost_usd: float
    suitability: int
    reasons: list[str]
    status: Literal["running", "starting", "new"]
    engine_version: str


class EstimateOut(BaseModel):
    model_id: str
    model_name: str
    est_cost_usd: float | None
    est_wait_s: float | None
    machine: MachineChoice | None
    alternatives: list[MachineChoice] = Field(default_factory=list)
    over_soft_cap: bool = False
    budget_left_usd: float | None = None
    message: str | None = None


class AssetOut(BaseModel):
    id: uuid.UUID
    media_type: str
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    bytes: int
    url: str
    download_url: str
    thumb_url: str | None = None


class JobOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_name: str | None = None
    kind: str
    mode: str
    model_id: str
    model_name: str
    prompt: str
    avoid: str | None = None
    lyrics: str | None = None
    params: dict[str, Any]
    state: str
    stage: str
    progress: float
    eta_s: float | None
    est_cost_usd: float | None
    est_wait_s: float | None
    cost_usd: float | None
    error: str | None
    machine: str | None = None
    instance_id: uuid.UUID | None = None
    adult: bool = False
    seed: int | None = None
    created_at: datetime
    finished_at: datetime | None
    outputs: list[AssetOut] = Field(default_factory=list)
    reference: AssetOut | None = None


class Page(BaseModel):
    items: list[JobOut]
    next_cursor: str | None = None


class InstanceOut(BaseModel):
    id: uuid.UUID
    name: str
    state: str
    gpu: str
    gpu_class: str
    vram_gb: float
    region: str
    region_name: str
    price_hourly: float
    created_at: datetime
    ready_at: datetime | None
    idle_since: datetime | None
    keep_warm_until: datetime | None
    idle_shutdown_at: datetime | None
    uptime_s: float | None
    cost_so_far_usd: float
    models_loaded: list[str]
    current_job_id: uuid.UUID | None = None
    failure_reason: str | None = None
    boot_stage: str | None = None  # current start-up step (while starting)
    boot_stage_label: str | None = None
    has_saved_log: bool = False  # last log lines were kept when the machine went away
    timeline: list[dict[str, Any]] = Field(default_factory=list)


class WarmupIn(BaseModel):
    kind: Literal["image", "video", "music"] = "image"
    model_id: str | None = None
    quality: Literal["draft", "standard", "best"] = "standard"
    offer_key: str | None = None
    preference: Literal["economy", "balanced", "fast"] | None = None


class KeepWarmIn(BaseModel):
    minutes: int = Field(ge=0, le=24 * 60)


class SpendingOut(BaseModel):
    period: str
    compute_usd: float
    reserved_usd: float
    storage_monthly_usd: float
    soft_cap_usd: float
    hard_cap_usd: float
    locked: bool
    provider_month_to_date_usd: float | None
    running_machines: int
    by_day: list[dict[str, Any]]
    recent_jobs: list[dict[str, Any]]
    by_machine: list[dict[str, Any]]
