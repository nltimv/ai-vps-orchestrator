"""SQLAlchemy ORM models. All timestamps are timezone-aware and set from DB ``now()``."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
    }


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> Any:
    return func.now()


# ------------------------------------------------------------------ users & auth


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(String(255))  # None for SSO-only accounts
    role: Mapped[str] = mapped_column(String(16), default="member")  # admin | member
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # may see and make adult content (also needs the global setting); see aivo/safety.py
    adult_allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    last_login_at: Mapped[datetime | None]


class UserIdentity(Base):
    """An external (OIDC) identity linked to a user: (issuer, subject) is the stable key."""

    __tablename__ = "user_identities"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    last_login_at: Mapped[datetime | None]

    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_user_identities_issuer_subject"),)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime] = mapped_column(server_default=_now())
    user_agent: Mapped[str | None] = mapped_column(String(300))


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="member")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    expires_at: Mapped[datetime]
    used_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    used_at: Mapped[datetime | None]


# ------------------------------------------------------------------ system & settings


class SystemRow(Base):
    __tablename__ = "system"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    deployment_id: Mapped[uuid.UUID] = mapped_column(default=_uuid)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    catalog_hash: Mapped[str | None] = mapped_column(String(64))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(server_default=_now(), onupdate=_now())


class ModelState(Base):
    __tablename__ = "model_state"
    model_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool | None] = mapped_column(Boolean)
    hot: Mapped[bool] = mapped_column(Boolean, default=False)
    mirror_status: Mapped[str] = mapped_column(String(16), default="none")  # none|mirroring|ready|failed
    mirrored_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    mirrored_at: Mapped[datetime | None]


class Lease(Base):
    __tablename__ = "leases"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    holder: Mapped[str] = mapped_column(String(128))
    epoch: Mapped[int] = mapped_column(BigInteger, default=0)
    expires_at: Mapped[datetime]
    last_tick_at: Mapped[datetime | None]


# ------------------------------------------------------------------ instances


class Instance(Base):
    __tablename__ = "instances"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_instance_id: Mapped[str | None] = mapped_column(String(64))
    offer_key: Mapped[str] = mapped_column(String(128))
    size_slug: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(32))
    gpu_class: Mapped[str] = mapped_column(String(32))
    gpu_model: Mapped[str] = mapped_column(String(64))
    vendor: Mapped[str] = mapped_column(String(16), default="nvidia")
    vram_gb: Mapped[float] = mapped_column(Float)
    ram_gb: Mapped[float] = mapped_column(Float, default=0)
    price_hourly: Mapped[Decimal] = mapped_column(Numeric(10, 5))
    image_ref: Mapped[str | None] = mapped_column(String(128))
    image_kind: Mapped[str | None] = mapped_column(String(16))  # snapshot | base
    worker_image: Mapped[str | None] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    public_ipv4: Mapped[str | None] = mapped_column(String(45))
    port: Mapped[int] = mapped_column(Integer, default=443)
    cert_ca_pem: Mapped[str | None] = mapped_column(Text)
    sni_name: Mapped[str | None] = mapped_column(String(128))
    token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    # RunPod: the endpoint (GPU type) this worker belongs to
    endpoint_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("gpu_endpoints.id", ondelete="SET NULL"))
    requested_by_job_id: Mapped[uuid.UUID | None]
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    files_present: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    models_loaded: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    drain_reason: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str | None] = mapped_column(String(16))  # ok | failed | external
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # start-up step reported by the machine (preparing_disks, downloading_software, starting_engine, ...)
    boot_stage: Mapped[str | None] = mapped_column(String(48))
    # last machine log lines, kept when a machine fails (the machine itself is destroyed)
    boot_log: Mapped[list[Any] | None] = mapped_column(JSONB)
    candidates: Mapped[list[Any]] = mapped_column(JSONB, default=list)  # ranked fallback list
    attempt_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    create_accepted_at: Mapped[datetime | None]
    ip_at: Mapped[datetime | None]
    ready_at: Mapped[datetime | None]
    idle_since: Mapped[datetime | None]
    keep_warm_until: Mapped[datetime | None]
    max_lifetime_at: Mapped[datetime | None]
    draining_at: Mapped[datetime | None]
    destroy_requested_at: Mapped[datetime | None]
    destroy_confirmed_at: Mapped[datetime | None]
    last_contact_at: Mapped[datetime | None]
    state_changed_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        Index("ix_instances_live", "state", postgresql_where="state <> 'destroyed'"),
        # a RunPod worker id can come back after it scaled down: unique only among live workers
        Index(
            "uq_instances_live_worker",
            "provider",
            "provider_instance_id",
            unique=True,
            postgresql_where="state <> 'destroyed'",
        ),
    )


class PlacementAttempt(Base):
    __tablename__ = "placement_attempts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instance_id: Mapped[uuid.UUID | None]
    rank: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String(32))
    size_slug: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(24))
    http_status: Mapped[int | None]
    error_text: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None]
    at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (Index("ix_placement_attempts_offer", "size_slug", "region", "at"),)


# ------------------------------------------------------------------ jobs & assets


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(8))
    model_id: Mapped[str] = mapped_column(String(64))
    variant_id: Mapped[str | None] = mapped_column(String(64))
    prompt_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    prompt_used_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # the improved description, if any
    avoid_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    lyrics_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    friendly_params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # adult content: only visible to accounts allowed to see it (aivo/safety.py)
    adult: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    resolved: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    input_asset_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    parent_job_id: Mapped[uuid.UUID | None]
    preference: Mapped[str] = mapped_column(String(16), default="balanced")
    placement_mode: Mapped[str] = mapped_column(String(8), default="auto")
    manual_offer_key: Mapped[str | None] = mapped_column(String(128))
    allow_fallback: Mapped[bool] = mapped_column(Boolean, default=True)
    min_vram_gb: Mapped[float | None] = mapped_column(Float)  # bumped after an out-of-memory failure
    state: Mapped[str] = mapped_column(String(20), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    instance_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("instances.id", ondelete="SET NULL"))
    # RunPod: the GPU type's endpoint, the queued RunPod job and the worker's one-time ticket (hash)
    endpoint_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("gpu_endpoints.id", ondelete="SET NULL"))
    gpu_type: Mapped[str | None] = mapped_column(String(64))
    runpod_job_id: Mapped[str | None] = mapped_column(String(80))
    endpoint_runpod_id: Mapped[str | None] = mapped_column(String(64))  # the endpoint that has the RunPod job
    ticket_hash: Mapped[str | None] = mapped_column(String(64))
    candidates: Mapped[list[Any]] = mapped_column(JSONB, default=list)  # GPU types to try, best first
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    log: Mapped[list[Any]] = mapped_column(JSONB, default=list)  # the worker's last log lines (admins)
    submitted_at: Mapped[datetime | None]
    worker_contact_at: Mapped[datetime | None]
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    eta_s: Mapped[float | None] = mapped_column(Float)
    est_cost_usd: Mapped[float | None] = mapped_column(Float)
    est_wait_s: Mapped[float | None] = mapped_column(Float)
    attributed_cost_usd: Mapped[float | None] = mapped_column(Float)
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    capacity_wait_until: Mapped[datetime | None]
    next_attempt_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    assigned_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]

    __table_args__ = (
        Index("ix_jobs_state_created", "state", "created_at"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_jobs_idempotency"),
    )


class Event(Base):
    """Append-only event log: feeds SSE (id = Last-Event-ID) and the audit trail."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16))  # job | instance | system
    ref_id: Mapped[uuid.UUID | None]
    user_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(server_default=_now(), index=True)


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    parent_asset_id: Mapped[uuid.UUID | None]  # thumbnail -> its output
    output_name: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))  # output | input | thumb
    media_type: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(String(256))
    plaintext_size: Mapped[int] = mapped_column(BigInteger)
    ciphertext_size: Mapped[int] = mapped_column(BigInteger)
    key_id: Mapped[str] = mapped_column(String(64))
    content_mac: Mapped[str] = mapped_column(String(64))
    width: Mapped[int | None]
    height: Mapped[int | None]
    duration_s: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    deleted_at: Mapped[datetime | None]


# ------------------------------------------------------------------ budget


class SpendPeriod(Base):
    __tablename__ = "spend_periods"
    period: Mapped[date] = mapped_column(Date, primary_key=True)
    soft_cap_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    hard_cap_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    locked: Mapped[bool] = mapped_column(Boolean, default=False)


class CostAccrual(Base):
    __tablename__ = "cost_accruals"
    instance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instances.id", ondelete="CASCADE"), primary_key=True)
    period: Mapped[date] = mapped_column(Date, primary_key=True)
    billed_seconds: Mapped[float] = mapped_column(Float, default=0)
    usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    updated_at: Mapped[datetime] = mapped_column(server_default=_now(), onupdate=_now())


# ------------------------------------------------------------------ RunPod endpoints


class GpuEndpoint(Base):
    """One RunPod queue-based endpoint per GPU type. Created on first use and kept: an idle endpoint is free,
    and RunPod keeps a worker ready on a host for it."""

    __tablename__ = "gpu_endpoints"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    gpu_type: Mapped[str] = mapped_column(String(64), unique=True)  # RunPod GPU type id
    gpu_name: Mapped[str] = mapped_column(String(64))
    gpu_class: Mapped[str | None] = mapped_column(String(32))
    vram_gb: Mapped[float] = mapped_column(Float, default=0)
    price_hourly: Mapped[Decimal] = mapped_column(Numeric(10, 5), default=0)
    runpod_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    config_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="creating")  # creating | ready | error
    error: Mapped[str | None] = mapped_column(Text)
    keep_warm_until: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(server_default=_now(), onupdate=_now())
    last_used_at: Mapped[datetime | None]


# ------------------------------------------------------------------ telemetry


class PhaseTiming(Base):
    __tablename__ = "phase_timings"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instance_id: Mapped[uuid.UUID | None]
    job_id: Mapped[uuid.UUID | None]
    phase: Mapped[str] = mapped_column(String(32))
    source: Mapped[str | None] = mapped_column(String(16))
    size_slug: Mapped[str | None] = mapped_column(String(64))
    gpu_class: Mapped[str | None] = mapped_column(String(32))
    region: Mapped[str | None] = mapped_column(String(32))
    image_kind: Mapped[str | None] = mapped_column(String(16))
    model_id: Mapped[str | None] = mapped_column(String(64))
    variant_id: Mapped[str | None] = mapped_column(String(64))
    quality: Mapped[str | None] = mapped_column(String(16))
    units: Mapped[float | None] = mapped_column(Float)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    seconds: Mapped[float] = mapped_column(Float)
    at: Mapped[datetime] = mapped_column(server_default=_now())


class Estimate(Base):
    __tablename__ = "estimates"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    ewma: Mapped[float] = mapped_column(Float)
    n: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(server_default=_now(), onupdate=_now())


class RegionProbe(Base):
    __tablename__ = "region_probes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32))
    region: Mapped[str] = mapped_column(String(32))
    rtt_ms: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(8))  # tcp | geo
    measured_at: Mapped[datetime] = mapped_column(server_default=_now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None]
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(server_default=_now())
