import { useQueryClient } from "@tanstack/react-query";
import { Check, Copy, HardDrive, Link2, Save } from "lucide-react";
import { useEffect, useState } from "react";

import { patch, post } from "@/api/client";
import { useCacheStatus, useCatalog, useHealth, useSaveSettings, useSettings, useUsers } from "@/api/queries";
import type { RuntimeSettings } from "@/api/types";
import { LicenseBadge } from "@/components/LicenseBadge";
import { Badge, Button, Card, Chips, ErrorNote, Field, Input, Label, PageHeader, Toggle } from "@/components/ui";
import { ago, cn, money } from "@/lib/format";

export function SettingsPage() {
  const s = useSettings();
  const save = useSaveSettings();
  const [draft, setDraft] = useState<RuntimeSettings | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => { if (s.data && !draft) setDraft(s.data.settings); }, [s.data, draft]);
  if (!s.data || !draft) return <div className="h-64 rounded-xl skeleton" />;

  const set = <K extends keyof RuntimeSettings>(k: K, v: RuntimeSettings[K]) => { setDraft({ ...draft, [k]: v }); setSaved(false); };
  const changed = Object.fromEntries(
    Object.entries(draft).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(s.data!.settings[k])),
  );
  const onSave = async () => { await save.mutateAsync(changed); setSaved(true); };
  const regions = s.data.regions;

  return (
    <div className="space-y-8">
      <PageHeader title="Settings" subtitle={`Cloud: ${s.data.provider === "fake" ? "simulated (no real machines)" : "DigitalOcean"}`}
        actions={<Button variant="primary" onClick={onSave} loading={save.isPending} disabled={!Object.keys(changed).length}>
          {saved ? <Check className="size-4" /> : <Save className="size-4" />} {saved ? "Saved" : "Save changes"}</Button>} />
      <ErrorNote error={save.error} />
      <Status />

      <Section title="Spending limits" desc="Machines stop starting once the monthly limit is reached.">
        <div className="grid gap-4 sm:grid-cols-2">
          <NumberField label="Ask before continuing above ($/month)" value={draft.soft_cap_usd} onChange={(v) => set("soft_cap_usd", v)} />
          <NumberField label="Monthly limit ($)" value={draft.hard_cap_usd} onChange={(v) => set("hard_cap_usd", v)} />
          <NumberField label="Most expensive machine allowed ($/hour)" value={draft.max_price_hourly} step={0.1} onChange={(v) => set("max_price_hourly", v)} />
        </div>
      </Section>

      <Section title="Machines" desc="How long machines stay on, and how many may run at once.">
        <div className="grid gap-4 sm:grid-cols-2">
          <NumberField label="Shut down after being idle for (minutes)" value={Math.round(draft.idle_timeout_s / 60)} min={1}
            onChange={(v) => set("idle_timeout_s", Math.round(v * 60))} />
          <NumberField label="Never run a machine longer than (hours)" value={draft.max_lifetime_s / 3600} step={0.5}
            onChange={(v) => set("max_lifetime_s", Math.round(v * 3600))} />
          <NumberField label="Machines at the same time" value={draft.max_instances} min={1} max={8} onChange={(v) => set("max_instances", Math.round(v))} />
          <NumberField label="Give up waiting for a free GPU after (minutes)" value={Math.round(draft.capacity_wait_max_s / 60)}
            onChange={(v) => set("capacity_wait_max_s", Math.round(v * 60))} />
        </div>
        <div className="mt-4"><Label>Default priority</Label>
          <Chips name="Default priority" value={draft.preference_default} onChange={(v) => v && set("preference_default", v)}
            options={[{ id: "economy", label: "Save money" }, { id: "balanced", label: "Balanced" }, { id: "fast", label: "Fastest" }]} /></div>
        <div className="mt-4"><Toggle checked={draft.speculative_warmup} onChange={(v) => set("speculative_warmup", v)}
          label="Start a machine as soon as someone opens the Create page (faster, costs a bit more)" /></div>
      </Section>

      <Section title="Locations" desc="Leave empty to allow every datacenter.">
        <Label>Allowed</Label>
        <RegionPicker regions={regions} value={draft.allowed_regions} onChange={(v) => set("allowed_regions", v)} />
        <div className="mt-4"><Label>Preferred</Label>
          <RegionPicker regions={regions} value={draft.preferred_regions} onChange={(v) => set("preferred_regions", v)} /></div>
      </Section>

      <Engines draft={draft} set={set} />
      <CacheSection draft={draft} set={set} spaces={s.data.spaces_configured} regions={regions} />

      <Section title="Security" desc="Who may connect to the machines, and what happens if this server disappears.">
        <Label>Machine access</Label>
        <Chips name="Machine access" value={draft.firewall_mode} onChange={(v) => v && set("firewall_mode", v)} options={[
          { id: "auto_egress_ip", label: "Only this server (automatic)" },
          { id: "cidrs", label: "Only these addresses" },
          { id: "open", label: "Anyone (certificate-protected)" },
        ]} />
        {draft.firewall_mode === "cidrs" && (
          <Input className="mt-3" placeholder="203.0.113.4/32, 198.51.100.0/24" value={draft.allowed_cidrs.join(", ")}
            onChange={(e) => set("allowed_cidrs", e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} />
        )}
        <div className="mt-4">
          <Toggle checked={draft.watchdog_enabled} onChange={(v) => set("watchdog_enabled", v)}
            label="Machines delete themselves if this server goes silent for 20 minutes" />
          {!s.data.watchdog_token_configured && draft.watchdog_enabled && (
            <p className="mt-2 text-xs text-amber-500">Needs AIVO_DO_WATCHDOG_TOKEN (a token that may only delete machines; use a dedicated cloud team).</p>
          )}
        </div>
      </Section>

      <Users />
    </div>
  );
}

function Section({ title, desc, children }: { title: string; desc?: string; children: React.ReactNode }) {
  return (
    <Card className="p-6">
      <h2 className="font-semibold">{title}</h2>
      {desc && <p className="mb-5 mt-1 text-sm text-muted">{desc}</p>}
      {children}
    </Card>
  );
}

function NumberField({ label, value, onChange, step = 1, min = 0, max }: { label: string; value: number; onChange: (v: number) => void; step?: number; min?: number; max?: number }) {
  return (
    <Field label={label}>
      <Input type="number" value={Number.isFinite(value) ? value : 0} step={step} min={min} max={max}
        onChange={(e) => onChange(Number(e.target.value))} /></Field>
  );
}

function RegionPicker({ regions, value, onChange }: { regions: { id: string; name: string }[]; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <div className="flex flex-wrap gap-2">
      {regions.map((r) => {
        const on = value.includes(r.id);
        return (
          <button key={r.id} type="button" onClick={() => onChange(on ? value.filter((x) => x !== r.id) : [...value, r.id])}
            className={cn("h-8 rounded-full border px-3 text-sm cursor-pointer", on ? "border-brand-500 bg-brand-500/15" : "border-base")}>
            {r.name}
          </button>
        );
      })}
    </div>
  );
}

function Engines({ draft, set }: { draft: RuntimeSettings; set: <K extends keyof RuntimeSettings>(k: K, v: RuntimeSettings[K]) => void }) {
  const cat = useCatalog();
  const all = cat.data?.models ?? [];
  const enabled = draft.enabled_models ?? all.filter((m) => m.enabled).map((m) => m.id);
  return (
    <Section title="Engines" desc="Which AI engines people can use.">
      <div className="grid gap-3 md:grid-cols-2">
        {all.map((m) => (
          <div key={m.id} className="flex items-start justify-between gap-3 rounded-lg border border-base p-3">
            <div>
              <div className="flex items-center gap-2 text-sm font-medium">{m.name} <LicenseBadge license={m.license} /></div>
              <div className="mt-0.5 text-xs text-muted">{m.tagline} · needs {m.min_vram_gb} GB+</div>
            </div>
            <Toggle checked={enabled.includes(m.id)} label=""
              onChange={(v) => set("enabled_models", v ? [...enabled, m.id] : enabled.filter((x) => x !== m.id))} />
          </div>
        ))}
      </div>
    </Section>
  );
}

function CacheSection({ draft, set, spaces, regions }: {
  draft: RuntimeSettings; set: <K extends keyof RuntimeSettings>(k: K, v: RuntimeSettings[K]) => void; spaces: boolean;
  regions: { id: string; name: string }[];
}) {
  const cache = useCacheStatus();
  const cat = useCatalog();
  const qc = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const cp = draft.cache_policy;
  const run = async (k: string, fn: () => Promise<unknown>) => {
    setBusy(k); setError(null);
    try { await fn(); await qc.invalidateQueries({ queryKey: ["cache"] }); } catch (e) { setError(e); } finally { setBusy(null); }
  };
  const enabledModels = (cat.data?.models ?? []).filter((m) => m.enabled).map((m) => m.id);
  return (
    <Section title="Faster start-up" desc="Keep things ready near the GPUs so new machines start quicker. These cost a little every month.">
      <ErrorNote error={error} />
      <div className="grid gap-4 md:grid-cols-3">
        <Card className="p-4">
          <div className="flex items-center gap-2 font-medium"><HardDrive className="size-4" /> Prepared machine image</div>
          <p className="mt-1 text-xs text-muted">Software pre-installed, so machines skip a ~5 minute setup. ≈ $2/month per location.</p>
          <Button size="sm" className="mt-3" disabled={!cache.data?.bake_supported} loading={busy === "bake"}
            onClick={() => run("bake", () => post("/admin/cache/bake"))}>Prepare now</Button>
        </Card>
        <Card className="p-4">
          <div className="font-medium">Model copy (Spaces)</div>
          <p className="mt-1 text-xs text-muted">{spaces ? "A private copy of the AI models for fast downloads. ≈ $5/month." : "Not set up (AIVO_SPACES_* settings)."}</p>
          <Button size="sm" className="mt-3" disabled={!spaces} loading={busy === "mirror"}
            onClick={() => run("mirror", () => post("/admin/cache/mirror", { models: enabledModels }))}>Copy all engines</Button>
        </Card>
        <Card className="p-4">
          <div className="font-medium">Model disks</div>
          <p className="mt-1 text-xs text-muted">Most-used models on a disk next to the GPUs. ≈ {money(cp.volume_size_gb * 0.1, 0)}/month per location.</p>
          <Toggle checked={cp.volume_enabled} onChange={(v) => set("cache_policy", { ...cp, volume_enabled: v })} label="Use model disks" />
          <Button size="sm" className="mt-3" disabled={!cp.volume_enabled} loading={busy === "vol"}
            onClick={() => run("vol", () => post("/admin/cache/volumes"))}>Create disks</Button>
        </Card>
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div><Label>Prepared image locations</Label>
          <RegionPicker regions={regions} value={cp.snapshot_regions} onChange={(v) => set("cache_policy", { ...cp, snapshot_regions: v })} /></div>
        <div><Label>Model disk locations</Label>
          <RegionPicker regions={regions} value={cp.volume_regions} onChange={(v) => set("cache_policy", { ...cp, volume_regions: v })} /></div>
      </div>
      {cache.data && (
        <div className="mt-5 text-sm">
          <div className="mb-2 font-medium">Currently: about {money(cache.data.monthly_total_usd, 2)}/month</div>
          <ul className="space-y-1 text-muted">
            {cache.data.resources.map((r) => (
              <li key={r.id}>{r.kind === "snapshot" ? "Prepared image" : r.kind === "volume" ? "Model disk" : r.kind} · {r.region} ·{" "}
                <Badge tone={r.status === "ready" ? "good" : "neutral"}>{r.status}</Badge> · {money(r.monthly_cost_usd, 2)}/month</li>
            ))}
            {cache.data.runs.slice(0, 3).map((r) => (
              <li key={r.id}>{r.kind} {r.state} ({r.step}) · {ago(r.started_at)}</li>
            ))}
          </ul>
        </div>
      )}
    </Section>
  );
}

function Users() {
  const users = useUsers();
  const qc = useQueryClient();
  const [link, setLink] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const invite = async (role: "member" | "admin") => {
    const r = await post<{ token: string }>("/auth/invites", { role });
    setLink(`${window.location.origin}/invite/${r.token}`);
    setCopied(false);
  };
  return (
    <Section title="People" desc="Everyone shares the gallery. Admins can change settings and machines.">
      <div className="mb-4 flex flex-wrap gap-2">
        <Button size="sm" onClick={() => invite("member")}><Link2 className="size-4" /> Invite someone</Button>
        <Button size="sm" variant="ghost" onClick={() => invite("admin")}>Invite an admin</Button>
      </div>
      {link && (
        <div className="mb-4 flex items-center gap-2 rounded-lg panel-2 p-2 text-sm">
          <code className="flex-1 truncate">{link}</code>
          <Button size="sm" onClick={() => { navigator.clipboard.writeText(link); setCopied(true); }}>
            {copied ? <Check className="size-4" /> : <Copy className="size-4" />} {copied ? "Copied" : "Copy"}
          </Button>
        </div>
      )}
      <div className="divide-y divide-[var(--border)]">
        {(users.data ?? []).map((u) => (
          <div key={u.id} className="flex items-center justify-between gap-2 py-2 text-sm">
            <span>{u.display_name} <span className="text-muted">· {u.email}</span> {u.role === "admin" && <Badge tone="brand">admin</Badge>}
              {!u.is_active && <Badge tone="bad">disabled</Badge>}</span>
            <Button size="sm" variant="ghost" onClick={async () => { await patch(`/admin/users/${u.id}`, { is_active: !u.is_active }); qc.invalidateQueries({ queryKey: ["users"] }); }}>
              {u.is_active ? "Disable" : "Enable"}
            </Button>
          </div>
        ))}
      </div>
    </Section>
  );
}

function Status() {
  const h = useHealth();
  if (!h.data) return null;
  const c = h.data.controller;
  return (
    <div className="flex flex-wrap gap-2 text-xs">
      <Badge tone={c?.alive ? "good" : "bad"}>{c?.alive ? `Machine manager running (checked ${Math.round(c.last_tick_age_s)} s ago)` : "Machine manager is not running"}</Badge>
      <Badge>version {h.data.version}</Badge>
      {h.data.offers_error && <Badge tone="warn">Cloud availability check failing</Badge>}
    </div>
  );
}
