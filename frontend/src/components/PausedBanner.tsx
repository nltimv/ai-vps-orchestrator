import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Play } from "lucide-react";
import { useState } from "react";

import { post } from "@/api/client";
import { useMachines } from "@/api/queries";
import { ago } from "@/lib/format";

import { Button, ErrorNote } from "./ui";

/** Shown while new machines are paused after repeated start-up failures. */
export function PausedBanner({ isAdmin }: { isAdmin: boolean }) {
  const machines = useMachines();
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const p = machines.data?.provisioning_paused;
  if (!p) return null;
  const resume = async () => {
    setBusy(true);
    setError(null);
    try {
      await post("/machines/provisioning/resume");
      await qc.invalidateQueries({ queryKey: ["machines"] });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div role="alert" className="mb-6 rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-1 gap-3">
          <AlertTriangle className="mt-0.5 size-5 shrink-0 text-amber-500" />
          <div className="min-w-0">
            <div className="font-medium">New machines are paused</div>
            <p className="mt-0.5 text-muted">
              The last {p.failures} machines failed to start, so aivo stopped starting new ones to avoid paying for more
              failed attempts ({ago(p.at)}). Machines that are already running keep working.
            </p>
            <p className="mt-2 break-words">Reason: {p.reason}</p>
            {isAdmin && (
              <p className="mt-1 text-muted">Open "Show log" on a recently shut down machine below for details, fix the cause, then resume.</p>
            )}
          </div>
        </div>
        {isAdmin && (
          <Button size="sm" onClick={resume} loading={busy}>
            <Play className="size-4" /> Resume start-ups
          </Button>
        )}
      </div>
      <ErrorNote error={error} />
    </div>
  );
}
