import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export const cn = (...v: ClassValue[]) => twMerge(clsx(v));

export function money(usd: number | null | undefined, digits?: number): string {
  if (usd === null || usd === undefined) return "—";
  const d = digits ?? (usd < 0.1 ? 3 : 2);
  return `$${usd.toFixed(d)}`;
}

export function duration(s: number | null | undefined): string {
  if (s === null || s === undefined) return "—";
  if (s < 60) return `${Math.max(1, Math.round(s))} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

export function until(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  return s <= 0 ? "now" : `in ${duration(s)}`;
}

const STAGES: Record<string, string> = {
  queued: "Waiting to start",
  waiting_in_line: "Waiting in line",
  waiting_for_gpu: "Starting a GPU",
  starting_machine: "Starting a GPU",
  loading_model: "Loading the AI model",
  generating: "Creating",
  saving: "Saving",
  done: "Done",
  failed: "Didn't work",
  cancelled: "Cancelled",
};
export const stageLabel = (stage: string) => STAGES[stage] ?? stage.replace(/_/g, " ");

const WORKER_STATES: Record<string, string> = {
  working: "Working",
  waiting: "Waiting for work",
  stopped: "Stopped",
};
export const workerStateLabel = (s: string) => WORKER_STATES[s] ?? s;


export const isActive = (state: string) => !["succeeded", "failed", "cancelled"].includes(state);

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

const PROVIDERS: Record<string, string> = {
  fake: "simulated RunPod (no real GPUs)",
  runpod: "RunPod Serverless",
};
export const providerLabel = (p: string) => PROVIDERS[p] ?? p;
