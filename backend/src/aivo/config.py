"""Configuration.

* ``EnvSettings``: process configuration from the environment (secrets, DSN, provider).
* ``RuntimeSettings``: admin-editable knobs stored in the DB ``settings`` table.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Lists from the environment may be given comma-separated ("a.com,b.com") or as JSON.
CsvList = Annotated[list[str], NoDecode]

REPO_ROOT = Path(__file__).resolve().parents[3]

Preference = Literal["economy", "balanced", "fast"]


RELEASE_VERSION = re.compile(r"^\d+\.\d+\.\d+([-.+][0-9A-Za-z.-]+)?$")


def release_tag() -> str:
    """The image tag of this release (e.g. 0.2.57); "latest" for a source checkout ("dev")."""
    from aivo import __version__

    return __version__ if RELEASE_VERSION.match(__version__) else "latest"


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

    # runpod = RunPod Serverless; fake = a simulated RunPod in this process (development, CI)
    provider: Literal["fake", "runpod"] = "fake"

    # --- RunPod Serverless ---------------------------------------------------------------
    # One queue-based endpoint per GPU type; RunPod's queue starts and stops the workers.
    # Each job's input is only a ticket: workers fetch the job from aivo and deliver results to aivo.
    runpod_api_key: SecretStr | None = None
    runpod_api_base: str = "https://api.runpod.io"  # REST API v2 (endpoints, GPU catalog)
    runpod_queue_base: str = "https://api.runpod.ai"  # job queue (/v2/<endpoint>/run, /status, /cancel)
    # Where workers may run: these countries (ISO codes) ...
    runpod_countries: CsvList = Field(
        default_factory=lambda: "NL,BE,DE,FR,CZ,RO,SE,NO,IS,FI,PL,ES,IT,AT,DK,IE,PT".split(",")
    )
    # ... or exactly these data centers (e.g. EU-NL-1,EU-RO-1); overrides the countries
    runpod_data_centers: CsvList = Field(default_factory=list)
    # Region name shown in aivo for RunPod GPUs
    runpod_region: str = "eu"
    # Only these GPU types (RunPod ids or names, e.g. "NVIDIA L40S,RTX 4090"); empty = all
    runpod_gpu_types: CsvList = Field(default_factory=list)
    # Optional network volume that keeps downloaded model files between workers (pins its data center)
    runpod_network_volume_id: str | None = None
    # Container disk for model files when there is no network volume (GB)
    runpod_disk_gb: int = 150
    # Address workers use to reach aivo (defaults to AIVO_PUBLIC_URL). Only /api/v1/worker/ is used,
    # with a one-time ticket per job.
    worker_api_url: str | None = None

    hf_token: SecretStr | None = None  # only used to resolve gated downloads backend-side

    # RunPod workers run the worker image of the same release as this backend, so an upgrade
    # changes both together. Set AIVO_WORKER_IMAGE only to pin another image.
    worker_image: str = ""
    worker_image_repository: str = "ghcr.io/nltimv/aivo-worker"

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

    # Simulated RunPod (AIVO_PROVIDER=fake): worker start-up and job time, in seconds
    fake_boot_seconds: float = 2.0
    fake_job_seconds: float = 2.0
    # Simulated problems for the next jobs, e.g. "worker_crash,no_gpu" (see providers/runpod/fake.py)
    fake_chaos: CsvList = Field(default_factory=list)

    log_level: str = "INFO"

    @field_validator(
        "oidc_allowed_domains",
        "oidc_allowed_groups",
        "oidc_admin_groups",
        "cors_origins",
        "fake_chaos",
        "runpod_countries",
        "runpod_data_centers",
        "runpod_gpu_types",
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

    @model_validator(mode="after")
    def _default_worker_image(self) -> EnvSettings:
        if not self.worker_image:
            self.worker_image = f"{self.worker_image_repository}:{release_tag()}"
        return self

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)


class RuntimeSettings(BaseModel):
    """Admin-editable settings with safe defaults."""

    preference_default: Preference = "balanced"
    denied_sizes: list[str] = Field(default_factory=list)  # GPU types not to use
    max_price_hourly: float = 10.0
    soft_cap_usd: float = 30.0
    hard_cap_usd: float = 40.0
    overrun_margin_usd: float = 2.0
    enabled_models: list[str] | None = None  # None = manifest defaults
    # RunPod: workers per GPU type at the same time (the endpoint's max workers)
    max_workers_per_gpu: int = 2
    # RunPod: a worker waits this long for a next job before it stops (billed while it waits)
    keep_warm_s: int = 60
    # No worker took the job within this time: try the next GPU type
    gpu_fallback_after_s: int = 300
    # Give up when no GPU took the job within this time
    capacity_wait_max_s: int = 1800
    # Longest a single creation may run on a worker
    job_timeout_s: int = 3600
    per_job_confirm_usd: float = 1.0
    # Adult (18+) engines and creations. Also needs the per-account switch (Settings -> People).
    adult_content_enabled: bool = False


@lru_cache
def get_env() -> EnvSettings:
    return EnvSettings()
