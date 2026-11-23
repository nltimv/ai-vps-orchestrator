// Mirrors backend/src/aivo/api/schemas.py

export type Kind = "image" | "video" | "music";
export type Quality = "draft" | "standard" | "best";
export type Aspect = "square" | "portrait" | "landscape" | "wide";
export type Preference = "economy" | "balanced" | "fast";

export interface AuthStatus {
  needs_setup: boolean;
  password_login: boolean;
  sso: { enabled: boolean; name: string };
}

export interface User {
  id: string;
  email: string;
  display_name: string;
  role: "admin" | "member";
  adult_allowed?: boolean;
  adult_content?: boolean;
}

export interface License {
  name: string;
  url: string;
  commercial: boolean;
  summary: string;
}

export interface CatalogModel {
  id: string;
  kind: Kind;
  name: string;
  tagline: string;
  description: string;
  modes: string[];
  enabled: boolean;
  license: License;
  supports: { avoid: boolean; seed: boolean; sound: boolean; lyrics: boolean; instrumental: boolean };
  durations: { id: string; label: string; seconds: number }[];
  max_count: number;
  min_vram_gb: number;
  tags: string[];
  adult?: boolean;
}

export interface Catalog {
  kinds: { id: Kind; label: string; modes: string[] }[];
  models: CatalogModel[];
  styles: { id: string; label: string }[];
  aspects: { id: Aspect; label: string; ratio: string }[];
  qualities: { id: Quality; label: string; hint: string }[];
  preferences: { id: Preference; label: string; hint: string }[];
  default_preference: Preference;
}

export interface CreateRequest {
  kind: Kind;
  mode?: string | null;
  model_id?: string | null;
  prompt: string;
  avoid?: string | null;
  style?: string | null;
  aspect?: Aspect | null;
  quality: Quality;
  duration?: string | null;
  count: number;
  sound?: boolean;
  lyrics?: string | null;
  instrumental?: boolean;
  seed?: number | null;
  reference_asset_id?: string | null;
  preference?: Preference | null;
  placement?: { mode: "auto" | "manual"; offer_key?: string | null; allow_fallback: boolean };
  confirm_over_soft_cap?: boolean;
}

export interface MachineChoice {
  key: string;
  instance_id: string | null;
  gpu: string;
  gpu_class: string | null;
  vram_gb: number;
  region: string;
  region_name: string;
  price_hourly: number;
  est_wait_s: number;
  est_cost_usd: number;
  suitability: number;
  reasons: string[];
  status: "running" | "starting" | "new";
  engine_version: "full" | "light";
}

export interface Estimate {
  model_id: string;
  model_name: string;
  est_cost_usd: number | null;
  est_wait_s: number | null;
  machine: MachineChoice | null;
  alternatives: MachineChoice[];
  over_soft_cap: boolean;
  budget_left_usd: number | null;
  message: string | null;
}

export interface Asset {
  id: string;
  media_type: string;
  width: number | null;
  height: number | null;
  duration_s: number | null;
  bytes: number;
  url: string;
  download_url: string;
  thumb_url: string | null;
}

export type JobState =
  | "queued" | "waiting_capacity" | "provisioning" | "assigned" | "preparing" | "running" | "fetching"
  | "succeeded" | "failed" | "cancelled";

export interface Job {
  id: string;
  user_id: string;
  user_name: string | null;
  kind: Kind;
  mode: string;
  model_id: string;
  model_name: string;
  prompt: string;
  avoid: string | null;
  lyrics: string | null;
  params: Record<string, unknown>;
  state: JobState;
  stage: string;
  progress: number;
  eta_s: number | null;
  est_cost_usd: number | null;
  est_wait_s: number | null;
  cost_usd: number | null;
  error: string | null;
  machine: string | null;
  instance_id?: string | null;
  adult?: boolean;
  seed: number | null;
  created_at: string;
  finished_at: string | null;
  outputs: Asset[];
  reference: Asset | null;
}

export interface GalleryPage {
  items: Job[];
  next_cursor: string | null;
}

export interface Instance {
  id: string;
  name: string;
  state: string;
  gpu: string;
  gpu_class: string;
  vram_gb: number;
  region: string;
  region_name: string;
  price_hourly: number;
  created_at: string;
  ready_at: string | null;
  idle_since: string | null;
  keep_warm_until: string | null;
  idle_shutdown_at: string | null;
  uptime_s: number | null;
  cost_so_far_usd: number;
  models_loaded: string[];
  current_job_id: string | null;
  failure_reason: string | null;
  boot_stage: string | null;
  boot_stage_label: string | null;
  has_saved_log: boolean;
  timeline: { at: string; state: string | null; detail: Record<string, unknown> }[];
}

export interface ProvisioningPause {
  paused: true;
  reason: string;
  at: string;
  failures: number;
}

export interface MachinesResponse {
  live: Instance[];
  recent: Instance[];
  provisioning_paused: ProvisioningPause | null;
}

export interface LogLine {
  ts: number;
  source: "aivo" | "setup" | "agent" | "engine" | string;
  level: string | null;
  text: string;
  seq?: number;
}

export interface LogStatus {
  phase: "live" | "waiting" | "unreachable" | "ending" | "ended";
  message: string;
  state?: string;
}

export interface OffersResponse {
  model_id: string;
  model_name: string;
  choices: MachineChoice[];
  unsuitable: { key: string; gpu: string; vram_gb: number; region: string; region_name: string; price_hourly: number; why: string }[];
  offers_error: string | null;
}

export interface Spending {
  period: string;
  compute_usd: number;
  reserved_usd: number;
  storage_monthly_usd: number;
  soft_cap_usd: number;
  hard_cap_usd: number;
  locked: boolean;
  provider_month_to_date_usd: number | null;
  running_machines: number;
  by_day: { day: string; usd: number }[];
  recent_jobs: { id: string; kind: Kind; model: string; user: string; state: string; created_at: string; est_cost_usd: number | null; cost_usd: number | null }[];
  by_machine: { id: string; gpu: string; region: string; state: string; usd: number; hours: number; created_at: string }[];
}

export interface CachePolicy {
  snapshot_regions: string[];
  volume_enabled: boolean;
  volume_regions: string[];
  volume_size_gb: number;
  hot_models: string[];
  mirror_enabled: boolean;
}

export interface RuntimeSettings {
  idle_timeout_s: number;
  min_billing_s: number;
  max_lifetime_s: number;
  lifetime_grace_s: number;
  max_instances: number;
  preference_default: Preference;
  allowed_regions: string[];
  preferred_regions: string[];
  denied_sizes: string[];
  max_price_hourly: number;
  soft_cap_usd: number;
  hard_cap_usd: number;
  overrun_margin_usd: number;
  boot_strategy: "auto" | "snapshot" | "base";
  cache_policy: CachePolicy;
  enabled_models: string[] | null;
  watchdog_enabled: boolean;
  firewall_mode: "auto_egress_ip" | "cidrs" | "open";
  allowed_cidrs: string[];
  speculative_warmup: boolean;
  capacity_wait_max_s: number;
  boot_failures_before_pause: number;
  boot_health_max_s: number;
  debug_ssh_cidrs: string[];
  adult_content_enabled: boolean;
  [k: string]: unknown;
}

export interface SettingsResponse {
  settings: RuntimeSettings;
  defaults: RuntimeSettings;
  regions: { id: string; name: string }[];
  provider: string;
  spaces_configured: boolean;
  watchdog_token_configured: boolean;
}

export interface CacheStatus {
  resources: { id: string; kind: string; region: string | null; status: string; size_gb: number | null; monthly_cost_usd: number; contents: number; in_use: boolean; created_at: string }[];
  mirror: { configured: boolean; stored_gb: number; monthly_cost_usd: number; models: Record<string, { status: string; gb: number }> };
  runs: {
    id: string;
    kind: string;
    state: string;
    step: string | null;
    stage?: string | null;
    error?: string | null;
    log_tail?: string[] | null;
    started_at: string;
    finished_at: string | null;
  }[];
  monthly_total_usd: number;
  bake_supported: boolean;
}

export interface Health {
  status: string;
  version: string;
  provider: string;
  controller: { holder: string; epoch: number; alive: boolean; last_tick_age_s: number } | null;
  offers_error: string | null;
  live_updates: { connected: boolean; error: string | null };
}

export interface ServerEvent {
  scope: "job" | "instance" | "system";
  ref_id: string | null;
  user_id: string | null;
  payload: Record<string, unknown>;
  at: string;
}
