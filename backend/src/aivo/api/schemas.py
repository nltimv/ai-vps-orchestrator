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
    prompt_used: str | None = None  # the improved description it was made with, if it was improved
    prompts_used: list[str | None] = Field(default_factory=list)  # per result (run), when each got its own
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
    outputs: list[AssetOut] = Field(default_factory=list)  # also while it runs: each batch as it's done
    expected: int | None = None  # how many results it makes, once known
    reference: AssetOut | None = None


class ItemOut(BaseModel):
    """One picture, video or song: a creation of its own in its owner's gallery. Several may have been made
    together in one run on the GPU (``job_id``)."""

    id: uuid.UUID  # the output file
    job_id: uuid.UUID
    kind: str
    index: int  # its place among those made together (0-based, counting deleted ones)
    together: int  # how many made together are still there, this one included
    media: AssetOut
    prompt: str
    favorite: bool = False
    adult: bool = False
    created_at: datetime


class ItemsPage(BaseModel):
    items: list[ItemOut]
    next_cursor: str | None = None


class ItemDetailOut(BaseModel):
    item: ItemOut
    job: JobOut  # how it was made; ``job.outputs`` are the ones made together
    improved: str | None = None  # this one's improved description
    seed: int | None = None  # this one's look number
    collection_ids: list[uuid.UUID] = Field(default_factory=list)


class FavoriteIn(BaseModel):
    favorite: bool


class ItemCollectionsIn(BaseModel):
    collection_ids: list[uuid.UUID] = Field(max_length=500)


class BulkIn(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    action: Literal["favorite", "unfavorite", "delete", "add", "remove"]
    collection_id: uuid.UUID | None = None  # for add / remove


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class CollectionOut(BaseModel):
    id: uuid.UUID
    name: str
    count: int
    cover_url: str | None = None  # the newest item's preview
    created_at: datetime
    updated_at: datetime


class AdminJobOut(BaseModel):
    """A creation as admins see it when troubleshooting: how it ran, never what it shows."""

    id: uuid.UUID
    user_name: str | None
    kind: str
    model_name: str
    state: str
    stage: str
    gpu_type: str | None
    error: str | None
    results: int
    cost_usd: float | None
    created_at: datetime
    finished_at: datetime | None


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
