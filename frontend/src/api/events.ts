import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import type { Job, ServerEvent } from "./types";

/** Live updates via Server-Sent Events: patch cached jobs, refresh lists. */
export function useServerEvents(enabled: boolean) {
  const qc = useQueryClient();
  useEffect(() => {
    if (!enabled) return;
    const es = new EventSource("/api/v1/events", { withCredentials: true });
    let refreshTimer: number | undefined;
    const refreshLists = () => {
      window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["jobs"] });
        qc.invalidateQueries({ queryKey: ["spending"] });
      }, 300);
    };
    const onJob = (ev: MessageEvent) => {
      const e: ServerEvent = JSON.parse(ev.data);
      if (!e.ref_id) return;
      const p = e.payload as Partial<Job> & { state?: string };
      qc.setQueryData<Job>(["job", e.ref_id], (old) => (old ? { ...old, ...p } as Job : old));
      qc.setQueryData<Job[]>(["jobs", "active"], (old) =>
        old?.map((j) => (j.id === e.ref_id ? ({ ...j, ...p } as Job) : j)),
      );
      if (p.state && ["succeeded", "failed", "cancelled", "provisioning", "assigned", "queued"].includes(p.state)) {
        qc.invalidateQueries({ queryKey: ["job", e.ref_id] });
        if (p.state === "succeeded") qc.invalidateQueries({ queryKey: ["gallery"] });
        refreshLists();
      }
    };
    const onInstance = () => {
      qc.invalidateQueries({ queryKey: ["machines"] });
      qc.invalidateQueries({ queryKey: ["offers"] });
    };
    for (const t of ["job.state", "job.progress", "job.created"]) es.addEventListener(t, onJob);
    for (const t of ["instance.state", "instance.keep_warm", "instance.lost", "boot_stage", "provisioning_paused", "provisioning_resumed"])
      es.addEventListener(t, onInstance);
    es.addEventListener("budget.locked", () => qc.invalidateQueries({ queryKey: ["spending"] }));
    for (const t of ["bake.started", "bake.progress", "bake.succeeded", "bake.failed", "cache.volume_created"])
      es.addEventListener(t, () => qc.invalidateQueries({ queryKey: ["cache"] }));
    return () => {
      window.clearTimeout(refreshTimer);
      es.close();
    };
  }, [enabled, qc]);
}
