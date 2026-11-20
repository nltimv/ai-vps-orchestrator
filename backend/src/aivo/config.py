"""Configuration.

* ``EnvSettings``: process configuration from the environment (secrets, DSN, provider).
* ``RuntimeSettings``: admin-editable knobs stored in the DB ``settings`` table.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

Preference = Literal["economy", "balanced", "fast"]


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVO_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://aivo:aivo@localhost:5432/aivo"
    role: Literal["api", "controller", "all"] = "all"

    # Encrypted media volume. This is the ONLY place generated content persists.
    media_dir: Path = Path("/var/lib/aivo/media")
    # RAM-backed scratch for transient plaintext (thumbnails, downloads before encryption).
    scratch_dir: Path = Path("/dev/shm/aivo")
    # "kid1:<base64 32 bytes>,kid2:<...>"
    media_keks: SecretStr = SecretStr("")
    media_active_key_id: str = ""

    catalog_dir: Path = REPO_ROOT / "catalog"

    provider: Literal["fake", "digitalocean"] = "fake"
    do_token: SecretStr | None = None
    do_api_base: str = "https://api.digitalocean.com"
    # Optional custom-scoped token (droplet:delete) handed to workers for the self-destruct watchdog.
    do_watchdog_token: SecretStr | None = None

    spaces_key: SecretStr | None = None
    spaces_secret: SecretStr | None = None
    spaces_region: str | None = None
    spaces_bucket: str | None = None
    spaces_endpoint: str | None = None  # override (e.g. MinIO in dev)

    hf_token: SecretStr | None = None  # only used to resolve gated downloads backend-side

    worker_image: str = "ghcr.io/nltimv/aivo-worker:latest"
    worker_port: int = 443

    admin_email: str | None = None
    admin_password: SecretStr | None = None

    session_cookie_secure: bool = False
    session_ttl_hours: int = 24 * 14
    cors_origins: list[str] = Field(default_factory=list)

    # Fake provider tuning (dev/CI)
    fake_boot_seconds: float = 2.0
    fake_job_seconds: float = 2.0

    log_level: str = "INFO"


class CachePolicy(BaseModel):
    snapshot_regions: list[str] = Field(default_factory=lambda: ["tor1", "nyc2"])
    volume_enabled: bool = True
    volume_regions: list[str] = Field(default_factory=lambda: ["tor1"])
    volume_size_gb: int = 120
    hot_models: list[str] = Field(default_factory=list)
    mirror_enabled: bool = True


class RuntimeSettings(BaseModel):
    """Admin-editable settings with safe defaults."""

    idle_timeout_s: int = 600
    min_billing_s: int = 300
    max_lifetime_s: int = 4 * 3600
    lifetime_grace_s: int = 900
    max_instances: int = 1
    preference_default: Preference = "balanced"
    allowed_regions: list[str] = Field(default_factory=list)  # empty = all
    preferred_regions: list[str] = Field(default_factory=list)
    denied_sizes: list[str] = Field(default_factory=list)
    max_price_hourly: float = 10.0
    soft_cap_usd: float = 30.0
    hard_cap_usd: float = 40.0
    overrun_margin_usd: float = 2.0
    boot_strategy: Literal["auto", "snapshot", "base"] = "auto"
    cache_policy: CachePolicy = Field(default_factory=CachePolicy)
    enabled_models: list[str] | None = None  # None = manifest defaults
    watchdog_enabled: bool = False
    firewall_mode: Literal["auto_egress_ip", "cidrs", "open"] = "auto_egress_ip"
    allowed_cidrs: list[str] = Field(default_factory=list)
    speculative_warmup: bool = False
    capacity_wait_max_s: int = 1800
    max_create_attempts: int = 8
    provision_deadline_s: int = 900
    boot_ip_timeout_s: int = 480
    boot_health_timeout_s: int = 600
    capacity_cooldown_s: int = 600
    per_job_confirm_usd: float = 1.0


@lru_cache
def get_env() -> EnvSettings:
    return EnvSettings()
