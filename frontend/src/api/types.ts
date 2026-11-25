// Mirrors backend/src/aivo/api/schemas.py

export type Kind = "image" | "video" | "music";
export type Quality = "draft" | "standard" | "high" | "best";
export type Aspect = "square" | "portrait" | "landscape" | "wide" | "9x16" | "3x4" | "4x5" | "4x3" | "21x9";
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
  improves: string[]; // modes in which "Improve my description" applies
  qualities: Quality[]; // the quality levels this engine has (without "high", High uses Standard)
  length: { min: number; max: number; step: number; default: number } | null; // seconds; null for pictures
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
  qualities: { id: Quality; label: string; hint: string; kinds: Kind[] }[]; // kinds: where the level is offered
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
  duration?: string | number | null; // seconds (or a preset key)
  count: number;
  sound?: boolean;
  lyrics?: string | null;
  instrumental?: boolean;
  seed?: number | null;
  reference_asset_id?: string | null;
  enhance?: boolean;
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
  prompt_used: string | null; // the improved description it was made with
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

/** One GPU type with its RunPod queue endpoint. */
export interface GpuEndpoint {
  gpu_type: string;
  gpu: string;
  gpu_class: string | null;
  vram_gb: number;
  price_hourly: number;
  status: "creating" | "ready" | "error";
  error: string | null;
  keep_warm_until: string | null;
  last_used_at: string | null;
  workers_running: number | null;
  workers_idle: number | null;
  jobs_waiting: number | null;
  jobs_active: number;
}

/** A RunPod worker, billed from its start until it stops. */
export interface Worker {
  id: string;
  worker_id: string;
  gpu: string;
  gpu_type: string;
  region_name: string;
  price_hourly: number;
  state: "working" | "waiting" | "stopped";
  started_at: string | null;
  idle_since: string | null;
  stops_at: string | null;
  ended_at: string | null;
  cost_usd: number;
  hours: number;
  current_job_id: string | null;
}

export interface GpusResponse {
  gpus: GpuEndpoint[];
  workers: Worker[];
  max_workers_per_gpu: number;
  keep_warm_s: number;
  offers_error: string | null;
}

export interface JobLog {
  state: string;
  stage: string;
  gpu_type: string | null;
  total: number;
  lines: LogLine[];
  error: string | null;
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

export interface RuntimeSettings {
  preference_default: Preference;
  denied_sizes: string[];
  max_price_hourly: number;
  soft_cap_usd: number;
  hard_cap_usd: number;
  overrun_margin_usd: number;
  enabled_models: string[] | null;
  max_workers_per_gpu: number;
  keep_warm_s: number;
  gpu_fallback_after_s: number;
  capacity_wait_max_s: number;
  job_timeout_s: number;
  adult_content_enabled: boolean;
  [k: string]: unknown;
}

export interface SettingsResponse {
  settings: RuntimeSettings;
  defaults: RuntimeSettings;
  provider: string;
  runpod: {
    network_volume: boolean;
    countries: string[];
    data_centers: string[];
    gpu_types: string[];
    worker_api_url: string | null;
  };
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
