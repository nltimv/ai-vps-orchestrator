import { useQueryClient } from "@tanstack/react-query";
import { Cpu, Flame, Power, ScrollText, Timer, Zap } from "lucide-react";
import { useState } from "react";

import { post } from "@/api/client";
import { useCatalog, useMachines, useOffers } from "@/api/queries";
import type { Instance, Kind, MachineChoice, Preference, Quality } from "@/api/types";
import { LogViewer } from "@/components/LogViewer";
import { PausedBanner } from "@/components/PausedBanner";
import { Badge, Button, Card, Chips, Empty, ErrorNote, Label, PageHeader } from "@/components/ui";
import { ago, cn, duration, machineStateLabel, money, until } from "@/lib/format";

export function MachinesPage({ isAdmin }: { isAdmin: boolean }) {
  const machines = useMachines();
  const catalog = useCatalog();
  const qc = useQueryClient();
  const [kind, setKind] = useState<Kind>("image");
  const [modelId, setModelId] = useState("");
  const [quality, setQuality] = useState<Quality>("standard");
  const [preference, setPreference] = useState<Preference>("balanced");
  const offers = useOffers(kind, modelId, quality, preference);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [logOf, setLogOf] = useState<Instance | null>(null);

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
  const live = machines.data?.live ?? [];
  const models = (catalog.data?.models ?? []).filter((m) => m.kind === kind && m.enabled);

  return (
    <div>
      <PageHeader title="Machines" subtitle="GPU machines start when there's work and shut down automatically when idle." />
      <PausedBanner isAdmin={isAdmin} />
      <ErrorNote error={error} />
      {logOf && <LogViewer machine={logOf} onClose={() => setLogOf(null)} />}
      <section className="mb-10">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">Running now</h2>
        {live.length === 0 ? (
          <Empty icon={<Cpu className="size-8" />} title="No machines running">You're not paying for anything right now.</Empty>
        ) : (
          <div className="grid gap-4 md:grid-cols-2">
            {live.map((m) => <MachineCard key={m.id} m={m} isAdmin={isAdmin} busy={busy} act={act} onLog={() => setLogOf(m)}
              names={Object.fromEntries((catalog.data?.models ?? []).map((x) => [x.id, x.name]))} />)}
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-muted">Machine options</h2>
        <p className="mb-4 text-sm text-muted">How well each available machine fits a typical creation. Availability changes often.</p>
        <Card className="mb-4 grid gap-4 p-4 md:grid-cols-4">
          <div><Label>For</Label>
            <Chips name="Kind" value={kind} onChange={(v) => { setKind(v ?? "image"); setModelId(""); }}
              options={[{ id: "image", label: "Images" }, { id: "video", label: "Videos" }, { id: "music", label: "Music" }]} /></div>
          <div><Label>Engine</Label>
            <select value={modelId} onChange={(e) => setModelId(e.target.value)} className="panel h-9 w-full rounded-lg px-2 text-sm">
              <option value="">Auto</option>
              {models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select></div>
          <div><Label>Quality</Label>
            <Chips name="Quality" value={quality} onChange={(v) => setQuality(v ?? "standard")}
              options={(catalog.data?.qualities ?? []).map((q) => ({ id: q.id, label: q.label }))} /></div>
          <div><Label>Priority</Label>
            <Chips name="Priority" value={preference} onChange={(v) => setPreference(v ?? "balanced")}
              options={(catalog.data?.preferences ?? []).map((p) => ({ id: p.id, label: p.label, hint: p.hint }))} /></div>
        </Card>
        {offers.data?.offers_error && <div className="mb-3 text-sm text-amber-500">Couldn't refresh availability: showing the last known list.</div>}
        <div className="space-y-2">
          {(offers.data?.choices ?? []).map((c, i) => (
            <OptionRow key={c.key} c={c} rank={i} isAdmin={isAdmin} busy={busy === c.key}
              onStart={() => act(c.key, () => post("/machines/warmup", { kind, model_id: modelId || null, quality, offer_key: c.key, preference }))} />
          ))}
          {offers.data && offers.data.choices.length === 0 && <Empty icon={<Cpu className="size-8" />} title="No suitable machines available right now" />}
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

      {(machines.data?.recent.length ?? 0) > 0 && (
        <section className="mt-10">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">Recently shut down</h2>
          <Card className="divide-y divide-[var(--border)]">
            {machines.data!.recent.map((m) => (
              <div key={m.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 text-sm">
                <span className="flex-1">{m.gpu} · {m.region_name}</span>
                <span className="text-muted">{ago(m.created_at)} · ran {duration(m.uptime_s)} · {money(m.cost_so_far_usd)}</span>
                {isAdmin && (
                  <Button size="sm" variant="ghost" onClick={() => setLogOf(m)} title={m.has_saved_log ? "Includes the machine's last log lines" : "aivo's own record of this machine"}>
                    <ScrollText className="size-4" /> Show log
                  </Button>
                )}
                {m.failure_reason && <div className="w-full text-red-500">{m.failure_reason}</div>}
              </div>
            ))}
          </Card>
        </section>
      )}
    </div>
  );
}

function MachineCard({ m, isAdmin, busy, act, names, onLog }: { m: Instance; isAdmin: boolean; busy: string | null; act: (k: string, fn: () => Promise<unknown>) => void; names: Record<string, string>; onLog: () => void }) {
  const starting = ["pending", "creating", "provisioning", "booting"].includes(m.state);
  // start-up journey only: first time each state was reached
  const journey = m.timeline.map((t) => t.state).filter((st, i, a): st is string => !!st && a.indexOf(st) === i && st !== "busy");
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 font-medium"><Cpu className="size-4" /> {m.gpu} <span className="text-muted">({m.vram_gb} GB)</span></div>
          <div className="mt-0.5 text-sm text-muted">{m.region_name} · {money(m.price_hourly, 2)}/hour</div>
        </div>
        <Badge tone={m.state === "busy" ? "brand" : m.state === "ready" ? "good" : starting ? "warn" : "neutral"}>
          {machineStateLabel(m.state)}
        </Badge>
      </div>
      <dl className="mt-4 grid grid-cols-2 gap-y-1 text-sm">
        {m.boot_stage_label && <><dt className="text-muted">Start-up step</dt><dd data-testid="boot-stage">{m.boot_stage_label}</dd></>}
        <dt className="text-muted">Running for</dt><dd>{duration(m.uptime_s)}</dd>
        <dt className="text-muted">Cost so far</dt><dd>{money(m.cost_so_far_usd)}</dd>
        {m.state === "ready" && <><dt className="text-muted">Shuts down</dt><dd className="flex items-center gap-1"><Timer className="size-3.5" />{until(m.idle_shutdown_at)}</dd></>}
        {m.models_loaded.length > 0 && <><dt className="text-muted">Ready to use</dt><dd className="truncate">{m.models_loaded.map((x) => names[x] ?? x).join(", ")}</dd></>}
      </dl>
      {journey.length > 0 && (
        <ol className="mt-3 flex flex-wrap items-center gap-1 text-[11px] text-muted">
          {journey.map((st, i) => <li key={st} className="rounded panel-2 px-1.5 py-0.5">{i > 0 && "→ "}{machineStateLabel(st)}</li>)}
        </ol>
      )}
      {isAdmin && (
        <div className="mt-4 flex flex-wrap gap-2">
          <Button size="sm" onClick={onLog}>
            <ScrollText className="size-4" /> Show log
          </Button>
          <Button size="sm" loading={busy === `warm-${m.id}`} onClick={() => act(`warm-${m.id}`, () => post(`/machines/${m.id}/keep-warm`, { minutes: 30 }))}>
            <Flame className="size-4" /> Keep warm 30 min
          </Button>
          <Button size="sm" variant="ghost" className="text-red-500" loading={busy === `off-${m.id}`}
            onClick={() => act(`off-${m.id}`, () => post(`/machines/${m.id}/shutdown`))}>
            <Power className="size-4" /> Shut down
          </Button>
        </div>
      )}
    </Card>
  );
}

function OptionRow({ c, rank, isAdmin, busy, onStart }: { c: MachineChoice; rank: number; isAdmin: boolean; busy: boolean; onStart: () => void }) {
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
          {c.status !== "new" && <Badge tone="good">{c.status === "running" ? "Running" : "Starting"}</Badge>}
        </div>
        <div className="mt-1 flex flex-wrap gap-1">
          {c.reasons.map((r) => <span key={r} className="rounded panel-2 px-2 py-0.5 text-xs text-muted">{r}</span>)}
        </div>
      </div>
      <div className="text-right text-sm">
        <div>{money(c.price_hourly, 2)}/h</div>
        <div className="text-xs text-muted">≈ {money(c.est_cost_usd)} · ~{duration(c.est_wait_s)}</div>
      </div>
      {isAdmin && c.status === "new" && <Button size="sm" onClick={onStart} loading={busy}>Start this one</Button>}
    </Card>
  );
}
