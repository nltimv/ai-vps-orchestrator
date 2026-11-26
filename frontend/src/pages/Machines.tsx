import { useQueryClient } from "@tanstack/react-query";
import { Cpu, Flame, ScrollText, Timer, Zap } from "lucide-react";
import { useState } from "react";

import { post } from "@/api/client";
import { useAdminJobs, useCatalog, useGpus, useOffers } from "@/api/queries";
import type { AdminJob, GpuEndpoint, Kind, MachineChoice, Preference, Quality, Worker } from "@/api/types";
import { KindIcon } from "@/components/JobCard";
import { LogViewer } from "@/components/LogViewer";
import { Badge, Button, Card, Chips, Empty, ErrorNote, Label, PageHeader } from "@/components/ui";
import { ago, cn, duration, isActive, money, stageLabel, until, workerStateLabel } from "@/lib/format";

export function MachinesPage({ isAdmin }: { isAdmin: boolean }) {
  const gpus = useGpus();
  const catalog = useCatalog();
  const qc = useQueryClient();
  const [kind, setKind] = useState<Kind>("image");
  const [modelId, setModelId] = useState("");
  const [quality, setQuality] = useState<Quality>("standard");
  const [preference, setPreference] = useState<Preference>("balanced");
  const offers = useOffers(kind, modelId, quality, preference);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    setError(null);
    try {
      await fn();
      await qc.invalidateQueries({ queryKey: ["machines"] });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  const keepReady = (gpuType: string, minutes: number) =>
    act(`warm-${gpuType}`, () => post(`/machines/gpus/${encodeURIComponent(gpuType)}/keep-warm`, { minutes }));
  const endpoints = gpus.data?.gpus ?? [];
  const workers = gpus.data?.workers ?? [];
  const live = workers.filter((w) => w.state !== "stopped");
  const stopped = workers.filter((w) => w.state === "stopped");
  const models = (catalog.data?.models ?? []).filter((m) => m.kind === kind && m.enabled);
  const keepWarm = gpus.data?.keep_warm_s ?? 60;

  return (
    <div>
      <PageHeader
        title="GPUs"
        subtitle="Creations run on RunPod. Each GPU type has a queue: workers start when there's work and stop soon after."
      />
      <ErrorNote error={error} />

      <section className="mb-10">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">Working now</h2>
        {live.length === 0 ? (
          <Empty icon={<Cpu className="size-8" />} title="No GPUs running">You're not paying for anything right now.</Empty>
        ) : (
          <div className="grid gap-4 md:grid-cols-2">
            {live.map((w) => <WorkerCard key={w.id} w={w} keepWarm={keepWarm} />)}
          </div>
        )}
      </section>

      {endpoints.length > 0 && (
        <section className="mb-10">
          <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-muted">GPU types in use</h2>
          <p className="mb-4 text-sm text-muted">
            Up to {gpus.data?.max_workers_per_gpu ?? 1} at a time per type. A worker waits {duration(keepWarm)} for a next
            creation before it stops.
          </p>
          <div className="space-y-2">
            {endpoints.map((g) => (
              <GpuRow key={g.gpu_type} g={g} isAdmin={isAdmin} busy={busy === `warm-${g.gpu_type}`}
                onKeepReady={(m) => keepReady(g.gpu_type, m)} />
            ))}
          </div>
        </section>
      )}

      {isAdmin && <EveryonesCreations />}

      <section>
        <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-muted">GPU options</h2>
        <p className="mb-4 text-sm text-muted">How well each GPU type fits a typical creation. Availability changes often.</p>
        <Card className="mb-4 grid gap-4 p-4 md:grid-cols-4">
          <div><Label>For</Label>
            <Chips name="Kind" value={kind} onChange={(v) => { setKind(v ?? "image"); setModelId(""); if (quality === "high") setQuality("standard"); }}
              options={[{ id: "image", label: "Images" }, { id: "video", label: "Videos" }, { id: "music", label: "Music" }]} /></div>
          <div><Label>Engine</Label>
            <select value={modelId} onChange={(e) => setModelId(e.target.value)} className="panel h-9 w-full rounded-lg px-2 text-sm">
              <option value="">Auto</option>
              {models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select></div>
          <div><Label>Quality</Label>
            <Chips name="Quality" value={quality} onChange={(v) => setQuality(v ?? "standard")}
              options={(catalog.data?.qualities ?? []).filter((q) => q.kinds.includes(kind)).map((q) => ({ id: q.id, label: q.label }))} /></div>
          <div><Label>Priority</Label>
            <Chips name="Priority" value={preference} onChange={(v) => setPreference(v ?? "balanced")}
              options={(catalog.data?.preferences ?? []).map((p) => ({ id: p.id, label: p.label, hint: p.hint }))} /></div>
        </Card>
        {offers.data?.offers_error && <div className="mb-3 text-sm text-amber-500">Couldn't refresh availability: showing the last known list.</div>}
        <div className="space-y-2">
          {(offers.data?.choices ?? []).map((c, i) => (
            <OptionRow key={c.key} c={c} rank={i} isAdmin={isAdmin} busy={busy === `warm-${gpuTypeOf(c)}`}
              onKeepReady={() => keepReady(gpuTypeOf(c), 30)} />
          ))}
          {offers.data && offers.data.choices.length === 0 && <Empty icon={<Cpu className="size-8" />} title="No suitable GPUs available right now" />}
        </div>
        {offers.data && offers.data.unsuitable.length > 0 && (
          <details className="mt-4 text-sm">
            <summary className="cursor-pointer text-muted">Not suitable ({offers.data.unsuitable.length})</summary>
            <ul className="mt-2 space-y-1">
              {offers.data.unsuitable.map((u) => (
                <li key={u.key} className="text-muted">{u.gpu} ({u.vram_gb} GB) in {u.region_name} — {u.why}</li>
              ))}
            </ul>
          </details>
        )}
      </section>

      {stopped.length > 0 && (
        <section className="mt-10">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">Stopped (last 24 hours)</h2>
          <Card className="divide-y divide-[var(--border)]">
            {stopped.map((w) => (
              <div key={w.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 text-sm">
                <span className="flex-1">{w.gpu} · {w.region_name}</span>
                <span className="text-muted">{ago(w.started_at)} · ran {duration(w.hours * 3600)} · {money(w.cost_usd)}</span>
              </div>
            ))}
          </Card>
        </section>
      )}
    </div>
  );
}

/** The offer key is "runpod:<GPU type>:<region>"; the GPU type itself may contain spaces. */
const gpuTypeOf = (c: MachineChoice) => c.key.split(":").slice(1, -1).join(":");

function WorkerCard({ w, keepWarm }: { w: Worker; keepWarm: number }) {
  return (
    <Card className="p-5" data-testid="worker">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 font-medium"><Cpu className="size-4" /> {w.gpu}</div>
          <div className="mt-0.5 text-sm text-muted">{w.region_name} · {money(w.price_hourly, 2)}/hour</div>
        </div>
        <Badge tone={w.state === "working" ? "brand" : "good"}>{workerStateLabel(w.state)}</Badge>
      </div>
      <dl className="mt-4 grid grid-cols-2 gap-y-1 text-sm">
        <dt className="text-muted">Running for</dt><dd>{duration(w.hours * 3600)}</dd>
        <dt className="text-muted">Cost so far</dt><dd>{money(w.cost_usd)}</dd>
        {w.state === "waiting" && (
          <><dt className="text-muted">Stops</dt><dd className="flex items-center gap-1"><Timer className="size-3.5" />{w.stops_at ? until(w.stops_at) : `within ${duration(keepWarm)}`}</dd></>
        )}
      </dl>
    </Card>
  );
}

/** Admins troubleshooting: everyone's recent creations as they ran (never their words or results). */
function EveryonesCreations() {
  const q = useAdminJobs(true);
  const qc = useQueryClient();
  const [log, setLog] = useState<AdminJob | null>(null);
  const [stopping, setStopping] = useState<string | null>(null);
  const rows = q.data ?? [];
  if (rows.length === 0) return null;
  const stop = async (id: string) => {
    setStopping(id);
    try {
      await post(`/admin/jobs/${id}/cancel`);
      await qc.invalidateQueries({ queryKey: ["admin-jobs"] });
    } finally {
      setStopping(null);
    }
  };
  return (
    <section className="mb-10">
      <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-muted">Everyone's creations</h2>
      <p className="mb-4 text-sm text-muted">
        For troubleshooting: how each one ran. Galleries are private, so descriptions and results aren't shown here.
      </p>
      <Card className="divide-y divide-[var(--border)]" data-testid="everyones-creations">
        {rows.map((j) => (
          <div key={j.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 text-sm">
            <KindIcon kind={j.kind} className="size-4 text-muted" />
            <span className="min-w-40 flex-1">
              {j.user_name} · {j.model_name}
              <span className="text-muted"> · {ago(j.created_at)}{j.gpu_type ? ` · ${j.gpu_type.replace(/^NVIDIA (GeForce )?/, "")}` : ""}</span>
              {j.error && <span className="block text-xs text-red-500">{j.error}</span>}
            </span>
            <Badge tone={j.state === "succeeded" ? "good" : j.state === "failed" ? "bad" : isActive(j.state) ? "brand" : "neutral"}>
              {j.state === "succeeded" ? `Done · ${j.results}` : j.state === "failed" ? "Failed" : j.state === "cancelled" ? "Cancelled" : stageLabel(j.stage)}
            </Badge>
            <span className="w-16 text-right text-muted">{money(j.cost_usd)}</span>
            <Button size="sm" variant="ghost" aria-label="Show log" title="The worker's log" onClick={() => setLog(j)}><ScrollText className="size-4" /></Button>
            {isActive(j.state) && (
              <Button size="sm" variant="ghost" loading={stopping === j.id} onClick={() => stop(j.id)}>Stop</Button>
            )}
          </div>
        ))}
      </Card>
      {log && <LogViewer jobId={log.id} label={`${log.model_name} · ${log.user_name ?? ""}`} onClose={() => setLog(null)} />}
    </section>
  );
}

function GpuRow({ g, isAdmin, busy, onKeepReady }: { g: GpuEndpoint; isAdmin: boolean; busy: boolean; onKeepReady: (minutes: number) => void }) {
  const warm = !!g.keep_warm_until;
  return (
    <Card className="flex flex-wrap items-center gap-4 p-4" data-testid="gpu-type">
      <div className="min-w-48 flex-1">
        <div className="flex items-center gap-2 font-medium">
          {g.gpu} <span className="text-sm text-muted">{g.vram_gb} GB · {money(g.price_hourly, 2)}/h while working</span>
          {g.status === "error" && <Badge tone="warn">Problem</Badge>}
          {warm && <Badge tone="good"><Flame className="size-3" /> Kept ready {until(g.keep_warm_until)}</Badge>}
        </div>
        <div className="mt-1 text-xs text-muted">
          {(g.workers_running ?? 0) + (g.workers_idle ?? 0)} running · {g.jobs_waiting ?? 0} waiting
          {g.last_used_at ? ` · last used ${ago(g.last_used_at)}` : ""}
        </div>
        {g.error && <div className="mt-1 text-xs text-red-500">{g.error}</div>}
      </div>
      {isAdmin && (
        warm
          ? <Button size="sm" variant="ghost" loading={busy} onClick={() => onKeepReady(0)}>Stop keeping ready</Button>
          : <Button size="sm" loading={busy} onClick={() => onKeepReady(30)} title="One worker stays on (billed all the time): no start-up wait">
              <Flame className="size-4" /> Keep one ready 30 min
            </Button>
      )}
    </Card>
  );
}

function OptionRow({ c, rank, isAdmin, busy, onKeepReady }: { c: MachineChoice; rank: number; isAdmin: boolean; busy: boolean; onKeepReady: () => void }) {
  return (
    <Card className={cn("flex flex-wrap items-center gap-4 p-4", rank === 0 && "border-brand-500")}>
      <div className="w-12 text-center">
        <div className="text-lg font-semibold">{c.suitability}</div>
        <div className="text-[10px] uppercase text-muted">fit</div>
      </div>
      <div className="min-w-48 flex-1">
        <div className="flex items-center gap-2 font-medium">
          {c.gpu} <span className="text-sm text-muted">{c.vram_gb} GB · {c.region_name}</span>
          {rank === 0 && <Badge tone="brand"><Zap className="size-3" /> Best pick</Badge>}
          {c.status === "running" && <Badge tone="good">On now</Badge>}
        </div>
        <div className="mt-1 flex flex-wrap gap-1">
          {c.reasons.map((r) => <span key={r} className="rounded panel-2 px-2 py-0.5 text-xs text-muted">{r}</span>)}
        </div>
      </div>
      <div className="text-right text-sm">
        <div>{money(c.price_hourly, 2)}/h</div>
        <div className="text-xs text-muted">≈ {money(c.est_cost_usd)} · ~{duration(c.est_wait_s)}</div>
      </div>
      {isAdmin && c.status !== "running" && (
        <Button size="sm" variant="ghost" onClick={onKeepReady} loading={busy} title="One worker stays on (billed all the time) for 30 minutes">
          Keep one ready
        </Button>
      )}
    </Card>
  );
}
