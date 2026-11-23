"""Configuration.

* ``EnvSettings``: process configuration from the environment (secrets, DSN, provider).
* ``RuntimeSettings``: admin-editable knobs stored in the DB ``settings`` table.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Lists from the environment may be given comma-separated ("a.com,b.com") or as JSON.
CsvList = Annotated[list[str], NoDecode]

REPO_ROOT = Path(__file__).resolve().parents[3]

Preference = Literal["economy", "balanced", "fast"]


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVO_", env_file=".env", extra="ignore", env_ignore_empty=True)

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
    # Operating-system image for GPU machines without a prepared image. DigitalOcean's GPU images come with
    # the GPU driver; a plain Ubuntu image has none, and the AI engine can't start there.
    do_base_image: str = "gpu-h100x1-base"
    do_base_image_amd: str = "gpu-amd-base"
    # Debugging only: your own DigitalOcean SSH key ids/fingerprints, added to every machine
    # (also open port 22 with Settings -> debug_ssh_cidrs).
    do_ssh_key_ids: CsvList = Field(default_factory=list)

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

    # Public base URL of the dashboard (e.g. https://aivo.example.com). Used to build the SSO redirect URI;
    # when unset it is derived from the incoming request (honouring X-Forwarded-* from the proxy).
    public_url: str | None = None
    # Allow signing in with email + password (set false for SSO-only).
    password_login: bool = True

    # --- Single sign-on (OpenID Connect) -------------------------------------------------
    oidc_issuer: str | None = None  # e.g. https://sso.example.com/realms/home
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None  # omit for a public client (PKCE only)
    oidc_scopes: str = "openid email profile"
    oidc_name: str = "SSO"  # shown on the button: "Sign in with <name>"
    oidc_auto_provision: bool = True  # create accounts for new SSO users (else: invite only)
    oidc_link_by_email: bool = True  # link to an existing account with the same *verified* email
    oidc_allowed_domains: CsvList = Field(default_factory=list)  # only these email domains may sign in
    oidc_allowed_groups: CsvList = Field(default_factory=list)  # only members of these groups may sign in
    oidc_admin_groups: CsvList = Field(default_factory=list)  # members become admins (role managed by SSO)
    oidc_groups_claim: str = "groups"

    session_cookie_secure: bool = False
    session_ttl_hours: int = 24 * 14
    cors_origins: CsvList = Field(default_factory=list)

    # Fake provider tuning (dev/CI)
    fake_boot_seconds: float = 2.0
    fake_job_seconds: float = 2.0
    # Simulated problems for the next machines, e.g. "boot_reports_failure,boot_reports_failure"
    # (see providers/fake/provider.py for the list). Dev only.
    fake_chaos: CsvList = Field(default_factory=list)

    log_level: str = "INFO"

    @field_validator(
        "oidc_allowed_domains",
        "oidc_allowed_groups",
        "oidc_admin_groups",
        "cors_origins",
        "do_ssh_key_ids",
        "fake_chaos",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json

                return json.loads(v)
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)


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
    # Time for a machine's software to come up. Extended while it reports progress, up to boot_health_max_s.
    boot_health_timeout_s: int = 600  # from a prepared machine image
    boot_health_timeout_base_s: int = 1500  # from the plain GPU image (installs and downloads everything)
    boot_health_max_s: int = 2400
    boot_progress_grace_s: int = 300  # each sign of start-up progress allows this much more time
    # Pause new machines after this many start-up failures in a row (0 = never).
    boot_failures_before_pause: int = 2
    requeue_backoff_s: int = 30  # pause before a job gets another machine after one failed
    debug_ssh_cidrs: list[str] = Field(default_factory=list)  # opens SSH (port 22) to these addresses
    capacity_cooldown_s: int = 600
    per_job_confirm_usd: float = 1.0
    # Adult (18+) engines and creations. Also needs the per-account switch (Settings -> People).
    adult_content_enabled: bool = False


@lru_cache
def get_env() -> EnvSettings:
    return EnvSettings()
