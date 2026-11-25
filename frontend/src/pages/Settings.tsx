import { useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Link2, Save } from "lucide-react";
import { useEffect, useState } from "react";

import { patch, post } from "@/api/client";
import { useCatalog, useHealth, useSaveSettings, useSettings, useUsers } from "@/api/queries";
import type { RuntimeSettings } from "@/api/types";
import { LicenseBadge } from "@/components/LicenseBadge";
import { Badge, Button, Card, Chips, ErrorNote, Field, Input, Label, PageHeader, Toggle } from "@/components/ui";
import { providerLabel } from "@/lib/format";

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
  const rp = s.data.runpod;

  return (
    <div className="space-y-8">
      <PageHeader title="Settings" subtitle={`GPUs: ${providerLabel(s.data.provider)}`}
        actions={<Button variant="primary" onClick={onSave} loading={save.isPending} disabled={!Object.keys(changed).length}>
          {saved ? <Check className="size-4" /> : <Save className="size-4" />} {saved ? "Saved" : "Save changes"}</Button>} />
      <ErrorNote error={save.error} />
      <Status />

      <Section title="Spending limits" desc="Nothing new starts once the monthly limit is reached.">
        <div className="grid gap-4 sm:grid-cols-2">
          <NumberField label="Ask before continuing above ($/month)" value={draft.soft_cap_usd} onChange={(v) => set("soft_cap_usd", v)} />
          <NumberField label="Monthly limit ($)" value={draft.hard_cap_usd} onChange={(v) => set("hard_cap_usd", v)} />
          <NumberField label="Most expensive GPU allowed ($/hour)" value={draft.max_price_hourly} step={0.1} onChange={(v) => set("max_price_hourly", v)} />
        </div>
      </Section>

      <Section title="GPUs" desc="Creations run on RunPod Serverless: you pay for a GPU only while it works (and while it waits briefly for a next creation).">
        <div className="grid gap-4 sm:grid-cols-2">
          <NumberField label="GPUs of one type at the same time" value={draft.max_workers_per_gpu} min={1} max={20}
            onChange={(v) => set("max_workers_per_gpu", Math.round(v))} />
          <NumberField label="Keep a GPU waiting for a next creation (seconds)" value={draft.keep_warm_s} min={5} max={3600}
            onChange={(v) => set("keep_warm_s", Math.round(v))} />
          <NumberField label="Try another GPU type if none is free after (minutes)" value={Math.round(draft.gpu_fallback_after_s / 60)} min={1}
            onChange={(v) => set("gpu_fallback_after_s", Math.round(v * 60))} />
          <NumberField label="Give up waiting for a free GPU after (minutes)" value={Math.round(draft.capacity_wait_max_s / 60)}
            onChange={(v) => set("capacity_wait_max_s", Math.round(v * 60))} />
          <NumberField label="Longest a single creation may take (minutes)" value={Math.round(draft.job_timeout_s / 60)} min={5}
            onChange={(v) => set("job_timeout_s", Math.round(v * 60))} />
        </div>
        <div className="mt-4"><Label>Default priority</Label>
          <Chips name="Default priority" value={draft.preference_default} onChange={(v) => v && set("preference_default", v)}
            options={[{ id: "economy", label: "Save money" }, { id: "balanced", label: "Balanced" }, { id: "fast", label: "Fastest" }]} /></div>
        <ListInput className="mt-4" label="Never use these GPU types" placeholder="NVIDIA H100 80GB HBM3, NVIDIA RTX A5000"
          value={draft.denied_sizes} onChange={(v) => set("denied_sizes", v)} />
        <ul className="mt-4 list-disc space-y-1 pl-5 text-sm text-muted">
          <li>Waiting for a free GPU is free; billing starts when a GPU starts working on your creation.</li>
          <li>{rp.data_centers.length ? `Data centers: ${rp.data_centers.join(", ")}.` : `Countries: ${rp.countries.join(", ") || "any"}.`}
            {rp.gpu_types.length ? ` Only: ${rp.gpu_types.join(", ")}.` : ""} (AIVO_RUNPOD_* settings)</li>
          <li>{rp.network_volume
            ? "Model files are kept on your RunPod network volume, so GPUs start faster."
            : "Each GPU downloads its model files when it starts. A RunPod network volume (AIVO_RUNPOD_NETWORK_VOLUME_ID) keeps them."}</li>
          <li>GPUs deliver results straight to {rp.worker_api_url ?? "this server (set AIVO_PUBLIC_URL)"}; nothing is stored at RunPod.</li>
        </ul>
      </Section>

      <Engines draft={draft} set={set} />

      <Section title="Adult content (18+)" desc="Engines that make nudity and sexual content, for adults only.">
        <Toggle checked={draft.adult_content_enabled} onChange={(v) => set("adult_content_enabled", v)}
          label="Allow adult content for the people switched on below" />
        <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-muted">
          <li>Only people with "18+" switched on under People see the adult engines and anything made with them. Admins too.</li>
          <li>Everyone else never sees adult creations: not in the gallery, not in lists, not via a link.</li>
          <li>Adult engines only start from pictures made here from a text description, never from uploaded photos.</li>
          <li>Descriptions that could involve minors are always refused.</li>
        </ul>
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

const parseList = (t: string) => t.split(",").map((x) => x.trim()).filter(Boolean);

/** Comma-separated list; keeps what's typed (e.g. a trailing comma) while editing. */
function ListInput({ value, onChange, label, placeholder, className }: { value: string[]; onChange: (v: string[]) => void; label?: string; placeholder?: string; className?: string }) {
  const [text, setText] = useState(value.join(", "));
  useEffect(() => {
    if (parseList(text).join(",") !== value.join(",")) setText(value.join(", "));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return (
    <Input aria-label={label} className={className} placeholder={placeholder} value={text}
      onChange={(e) => { setText(e.target.value); onChange(parseList(e.target.value)); }} />
  );
}

function NumberField({ label, value, onChange, step = 1, min = 0, max }: { label: string; value: number; onChange: (v: number) => void; step?: number; min?: number; max?: number }) {
  return (
    <Field label={label}>
      <Input type="number" value={Number.isFinite(value) ? value : 0} step={step} min={min} max={max}
        onChange={(e) => onChange(Number(e.target.value))} /></Field>
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
    <Section title="People" desc="Everyone shares the gallery. Admins can change settings and GPUs.">
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
            <span>{u.display_name} <span className="text-muted">· {u.email}</span> {u.role === "admin" && <Badge tone="brand">admin</Badge>}{" "}
              {u.sso && <Badge tone="neutral">single sign-on</Badge>}
              {!u.is_active && <Badge tone="bad">disabled</Badge>}
              {u.adult_allowed && <Badge tone="bad" title="May see and make adult content (when allowed above)">18+</Badge>}</span>
            <span className="flex gap-1">
              <Button size="sm" variant="ghost" aria-label={`${u.adult_allowed ? "Switch off" : "Switch on"} 18+ for ${u.display_name}`}
                onClick={async () => {
                  await patch(`/admin/users/${u.id}`, { adult_allowed: !u.adult_allowed });
                  qc.invalidateQueries({ queryKey: ["users"] });
                  qc.invalidateQueries({ queryKey: ["me"] });
                  qc.invalidateQueries({ queryKey: ["catalog"] });
                }}>
                {u.adult_allowed ? "18+ off" : "18+ on"}
              </Button>
              <Button size="sm" variant="ghost" onClick={async () => { await patch(`/admin/users/${u.id}`, { is_active: !u.is_active }); qc.invalidateQueries({ queryKey: ["users"] }); }}>
                {u.is_active ? "Disable" : "Enable"}
              </Button>
            </span>
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
      <Badge tone={c?.alive ? "good" : "bad"}>{c?.alive ? `GPU manager running (checked ${Math.round(c.last_tick_age_s)} s ago)` : "GPU manager is not running"}</Badge>
      <Badge>version {h.data.version}</Badge>
      {h.data.offers_error && <Badge tone="warn">Cloud availability check failing</Badge>}
      {h.data.live_updates && !h.data.live_updates.connected && (
        <Badge tone="warn" title={h.data.live_updates.error ?? undefined}>
          Live updates unavailable — progress refreshes every few seconds
        </Badge>
      )}
    </div>
  );
}
