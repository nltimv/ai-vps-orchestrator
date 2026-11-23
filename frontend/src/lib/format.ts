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
  waiting_for_gpu: "Waiting for a free GPU",
  starting_machine: "Starting a machine",
  loading_model: "Loading the AI model",
  generating: "Creating",
  saving: "Saving",
  done: "Done",
  failed: "Didn't work",
  cancelled: "Cancelled",
};
export const stageLabel = (stage: string) => STAGES[stage] ?? stage.replace(/_/g, " ");

const MACHINE_STATES: Record<string, string> = {
  pending: "Getting ready",
  creating: "Requesting a machine",
  provisioning: "Machine is starting",
  booting: "Setting up software",
  ready: "Ready",
  busy: "Working",
  draining: "Finishing up",
  destroying: "Shutting down",
  destroyed: "Shut down",
};
export const machineStateLabel = (s: string) => MACHINE_STATES[s] ?? s;

const SETUP_STEPS: Record<string, string> = {
  starting: "starting up",
  preparing_disks: "preparing disks",
  checking_gpu: "checking the GPU",
  installing_runtime: "installing the container runtime",
  downloading_software: "downloading the worker software",
  checking_engine: "checking the worker software can use the GPU",
  cleaning_up: "cleaning up",
  starting_engine: "starting the AI engine",
};
export const setupStepLabel = (s: string) => SETUP_STEPS[s] ?? s.replace(/_/g, " ");

export const isActive = (state: string) => !["succeeded", "failed", "cancelled"].includes(state);

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
