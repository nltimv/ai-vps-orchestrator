import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Cpu, ImagePlus, Sparkles, Wand2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";

import { ApiError, post, upload } from "@/api/client";
import { useActiveJobs, useCatalog, useEstimate, useRecentJobs } from "@/api/queries";
import type { Asset, Aspect, CatalogModel, CreateRequest, Job, Kind, Preference, Quality } from "@/api/types";
import { JobCard, KindIcon } from "@/components/JobCard";
import { LicenseBadge } from "@/components/LicenseBadge";
import { Button, Card, Chips, ErrorNote, Label, Modal, Textarea, Toggle } from "@/components/ui";
import { cn, duration, money, newIdempotencyKey } from "@/lib/format";

const KIND_TABS: { id: Kind; label: string; placeholder: string }[] = [
  { id: "image", label: "Image", placeholder: "A cozy cabin in a snowy forest at dusk, warm light in the windows" },
  { id: "video", label: "Video", placeholder: "A drone shot flying over turquoise waves crashing on black volcanic rocks" },
  { id: "music", label: "Music", placeholder: "Upbeat synth-pop with driving drums, bright female vocals, summer vibes" },
];
const GENRES = ["Pop", "Rock", "Hip hop", "Lo-fi", "Electronic", "Jazz", "Classical", "Cinematic", "Acoustic", "R&B"];

export interface CreatePrefill {
  kind?: Kind;
  prompt?: string;
  reference?: Asset;
  seed?: number | null;
  params?: Record<string, unknown>;
  model_id?: string;
  lyrics?: string | null;
  avoid?: string | null;
}

export function CreatePage() {
  const qc = useQueryClient();
  const catalog = useCatalog();
  const active = useActiveJobs();
  const recent = useRecentJobs();
  const prefill = (useLocation().state ?? {}) as CreatePrefill;
  const p = prefill.params ?? {};

  const [kind, setKind] = useState<Kind>(prefill.kind ?? "image");
  const [prompt, setPrompt] = useState(prefill.prompt ?? "");
  const [style, setStyle] = useState<string | null>((p.style as string) ?? null);
  const [aspect, setAspect] = useState<Aspect | null>((p.aspect as Aspect) ?? null);
  const [quality, setQuality] = useState<Quality>((p.quality as Quality) ?? "standard");
  const [count, setCount] = useState<number>((p.count as number) ?? 1);
  const [durationKey, setDurationKey] = useState<string | null>((p.duration as string) ?? null);
  const [sound, setSound] = useState<boolean>((p.sound as boolean) ?? true);
  const [lyrics, setLyrics] = useState(prefill.lyrics ?? "");
  const [instrumental, setInstrumental] = useState<boolean>((p.instrumental as boolean) ?? false);
  const [avoid, setAvoid] = useState(prefill.avoid ?? "");
  const [seed, setSeed] = useState<number | null>(prefill.seed ?? null);
  const [modelId, setModelId] = useState<string>(prefill.model_id ?? "auto");
  const [preference, setPreference] = useState<Preference | null>(null);
  const [reference, setReference] = useState<Asset | null>(prefill.reference ?? null);
  const [genres, setGenres] = useState<string[]>([]);
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

  const models = useMemo(
    () => (catalog.data?.models ?? []).filter((m) => m.kind === kind && m.enabled),
    [catalog.data, kind],
  );
  const mode = kind === "image" ? (reference ? "edit" : "t2i") : kind === "video" ? (reference ? "i2v" : "t2v") : "t2m";
  const usable = models.filter((m) => m.modes.includes(mode));
  const selected: CatalogModel | undefined = modelId === "auto" ? undefined : usable.find((m) => m.id === modelId);
  const supports = selected?.supports ?? {
    avoid: usable.some((m) => m.supports.avoid), sound: usable.some((m) => m.supports.sound),
    lyrics: kind === "music", instrumental: kind === "music", seed: true,
  };
  const [routedId, setRoutedId] = useState<string | null>(null);
  const routed = selected ?? usable.find((m) => m.id === routedId) ?? usable[0];
  const durations = routed?.durations ?? [];
  const maxCount = routed?.max_count ?? 1;

  useEffect(() => {
    if (modelId !== "auto" && !usable.some((m) => m.id === modelId)) setModelId("auto");
  }, [usable, modelId]);
  useEffect(() => setCount((c) => Math.min(c, maxCount)), [maxCount]);

  const fullPrompt = kind === "music" && genres.length ? `${genres.join(", ")}. ${prompt}` : prompt;
  const body: CreateRequest = {
    kind, mode, model_id: modelId === "auto" ? null : modelId, prompt: fullPrompt.trim() || "preview",
    avoid: supports.avoid && avoid ? avoid : null, style: kind === "music" ? null : style,
    aspect: kind === "music" ? null : aspect, quality, duration: kind === "image" ? null : durationKey,
    count, sound, lyrics: kind === "music" && !instrumental ? lyrics || null : null, instrumental,
    seed, reference_asset_id: reference?.id ?? null, preference,
    placement: { mode: "auto", allow_fallback: true },
  };
  const { prompt: _ignored, ...estimateKey } = body;
  const estimate = useEstimate({ ...estimateKey, prompt: "estimate" }, !!catalog.data && usable.length > 0);
  useEffect(() => { if (estimate.data?.model_id) setRoutedId(estimate.data.model_id); }, [estimate.data?.model_id]);
  // speculative start (only does something when enabled in Settings and no machine is running)
  useEffect(() => {
    if (sessionStorage.getItem("aivo-prewarmed")) return;
    sessionStorage.setItem("aivo-prewarmed", "1");
    post("/machines/prewarm", { kind: "image", quality: "standard" }).catch(() => undefined);
  }, []);

  const submit = async (confirmed = false) => {
    if (!fullPrompt.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await post<Job>("/jobs", { ...body, prompt: fullPrompt.trim(), confirm_over_soft_cap: confirmed },
        { "Idempotency-Key": newIdempotencyKey() });
      setConfirm(null);
      qc.invalidateQueries({ queryKey: ["jobs"] });
    } catch (e) {
      if (e instanceof ApiError && e.code === "confirm_soft_cap") setConfirm(e.message);
      else setError(e);
    } finally {
      setBusy(false);
    }
  };

  const onFile = async (f: File | undefined) => {
    if (!f) return;
    setUploading(true);
    setError(null);
    try {
      setReference(await upload<Asset>("/uploads", f));
    } catch (e) {
      setError(e);
    } finally {
      setUploading(false);
    }
  };

  const est = estimate.data;
  const activeJobs = active.data ?? [];
  const doneJobs = (recent.data ?? []).filter((j) => !activeJobs.some((a) => a.id === j.id)).slice(0, 4);

  return (
    <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_380px]">
      <div>
        <h1 className="mb-1 text-2xl font-semibold tracking-tight">What do you want to create?</h1>
        <p className="mb-6 text-sm text-muted">Describe it in your own words. A machine starts only when needed and shuts down when you're done.</p>

        <div className="mb-5 inline-flex rounded-xl panel p-1" role="tablist">
          {KIND_TABS.map((t) => (
            <button key={t.id} role="tab" aria-selected={kind === t.id}
              onClick={() => { setKind(t.id); setDurationKey(null); if (t.id === "music") setReference(null); }}
              className={cn("flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-colors cursor-pointer",
                kind === t.id ? "brand-gradient text-white" : "hover:bg-[var(--panel-2)]")}>
              <KindIcon kind={t.id} className="size-4" /> {t.label}
            </button>
          ))}
        </div>

        <Card className="p-5">
          <Label>{kind === "music" ? "Describe the music" : "Describe what you want"}</Label>
          <Textarea rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)}
            placeholder={KIND_TABS.find((t) => t.id === kind)!.placeholder} aria-label="Description"
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }} />

          {kind !== "music" && (
            <div className="mt-4">
              {reference ? (
                <div className="flex items-center gap-3 rounded-lg panel-2 p-2">
                  <img src={reference.url} alt="Starting image" className="size-16 rounded-md object-cover" />
                  <div className="flex-1 text-sm">
                    <div className="font-medium">{kind === "image" ? "Editing this image" : "Animating this image"}</div>
                    <div className="text-xs text-muted">{kind === "image" ? "Say what should change." : "Describe the motion."}</div>
                  </div>
                  <Button size="sm" variant="ghost" onClick={() => setReference(null)} aria-label="Remove image"><X className="size-4" /></Button>
                </div>
              ) : (
                <label className="flex cursor-pointer items-center gap-2 rounded-lg border border-dashed border-base px-3 py-2.5 text-sm text-muted hover:bg-[var(--panel-2)]">
                  <ImagePlus className="size-4" />
                  {uploading ? "Uploading…" : kind === "image" ? "Start from an image (optional) — to edit it" : "Start from an image (optional) — to animate it"}
                  <input type="file" accept="image/png,image/jpeg,image/webp" className="hidden" onChange={(e) => onFile(e.target.files?.[0])} />
                </label>
              )}
            </div>
          )}

          {kind === "music" && (
            <div className="mt-4 space-y-4">
              <div>
                <Label>Genre</Label>
                <div className="flex flex-wrap gap-2">
                  {GENRES.map((g) => (
                    <button key={g} type="button" onClick={() => setGenres((x) => x.includes(g) ? x.filter((y) => y !== g) : [...x, g])}
                      className={cn("h-8 rounded-full border px-3 text-sm cursor-pointer", genres.includes(g) ? "border-brand-500 bg-brand-500/15" : "border-base")}>
                      {g}
                    </button>
                  ))}
                </div>
              </div>
              <Toggle checked={instrumental} onChange={setInstrumental} label="Instrumental (no vocals)" />
              {!instrumental && (
                <div>
                  <Label hint="optional — leave empty to let the AI write them">Lyrics</Label>
                  <Textarea rows={5} value={lyrics} onChange={(e) => setLyrics(e.target.value)}
                    placeholder={"[verse]\nWalking down the empty street...\n[chorus]\n..."} />
                </div>
              )}
            </div>
          )}
        </Card>

        <Card className="mt-4 space-y-5 p-5">
          {kind !== "music" && catalog.data && (
            <>
              <div><Label>Style</Label>
                <Chips name="Style" allowNone value={style} onChange={setStyle} options={catalog.data.styles} /></div>
              {!(kind === "image" && reference) && (
                <div><Label>Shape</Label>
                  <Chips name="Shape" value={aspect ?? (kind === "video" ? "landscape" : "square")} onChange={(v) => setAspect(v)}
                    options={catalog.data.aspects.map((a) => ({ id: a.id, label: `${a.label} ${a.ratio}` }))} /></div>
              )}
            </>
          )}
          {kind !== "image" && durations.length > 0 && (
            <div><Label>Length</Label>
              <Chips name="Length" value={durationKey ?? durations[0].id} onChange={setDurationKey}
                options={durations.map((d) => ({ id: d.id, label: d.label }))} /></div>
          )}
          {kind === "video" && supports.sound && (
            <Toggle checked={sound} onChange={setSound} label="Include sound (only some engines can)" />
          )}
          {catalog.data && (
            <div><Label>Quality</Label>
              <Chips name="Quality" value={quality} onChange={(v) => setQuality(v ?? "standard")} options={catalog.data.qualities} /></div>
          )}
          {maxCount > 1 && (
            <div><Label>How many</Label>
              <Chips name="How many" value={String(count)} onChange={(v) => setCount(Number(v ?? 1))}
                options={Array.from({ length: maxCount }, (_, i) => ({ id: String(i + 1), label: String(i + 1) }))} /></div>
          )}

          <button type="button" onClick={() => setMore(!more)} className="flex items-center gap-1 text-sm text-muted hover:text-[var(--text)] cursor-pointer">
            <ChevronDown className={cn("size-4 transition-transform", more && "rotate-180")} /> More options
          </button>
          {more && catalog.data && (
            <div className="space-y-5 border-t border-base pt-5">
              <div>
                <Label hint="Auto picks the best one for what you asked">Engine</Label>
                <div className="grid gap-2 sm:grid-cols-2">
                  <EngineOption active={modelId === "auto"} onClick={() => setModelId("auto")} title="Auto" subtitle="Let aivo choose" />
                  {usable.map((m) => (
                    <EngineOption key={m.id} active={modelId === m.id} onClick={() => setModelId(m.id)} title={m.name}
                      subtitle={m.tagline} badge={<LicenseBadge license={m.license} />} />
                  ))}
                </div>
              </div>
              <div><Label>Speed vs. price</Label>
                <Chips name="Speed vs price" value={preference ?? catalog.data.default_preference} onChange={setPreference}
                  options={catalog.data.preferences} /></div>
              {supports.avoid && (
                <div><Label hint="things you don't want to see">Avoid</Label>
                  <Textarea rows={2} value={avoid} onChange={(e) => setAvoid(e.target.value)} placeholder="blurry, text, extra fingers" /></div>
              )}
              <div className="flex items-center gap-3 text-sm">
                <Toggle checked={seed !== null} onChange={(v) => setSeed(v ? Math.floor(Math.random() * 2 ** 31) : null)} label="Keep this look" />
                <span className="text-xs text-muted">{seed !== null ? `Look #${seed}: same settings give the same result` : "Each creation is different"}</span>
              </div>
            </div>
          )}
        </Card>

        <div className="mt-4 flex flex-wrap items-center justify-between gap-4">
          <EstimateLine loading={estimate.isFetching} est={est} error={estimate.error} />
          <Button variant="primary" size="lg" onClick={() => submit()} loading={busy} disabled={!prompt.trim() || usable.length === 0}>
            <Wand2 className="size-5" /> Create
          </Button>
        </div>
        <ErrorNote error={error} />
      </div>

      <aside className="space-y-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">In progress</h2>
        {activeJobs.length === 0 && <div className="rounded-xl border border-dashed border-base p-6 text-center text-sm text-muted">Nothing running right now.</div>}
        {activeJobs.map((j) => <JobCard key={j.id} job={j} />)}
        {doneJobs.length > 0 && <h2 className="pt-4 text-sm font-semibold uppercase tracking-wide text-muted">Recently</h2>}
        {doneJobs.map((j) => <JobCard key={j.id} job={j} />)}
      </aside>

      <Modal open={!!confirm} onClose={() => setConfirm(null)} title="Budget check">
        <p className="mb-6 text-sm">{confirm}</p>
        <div className="flex justify-end gap-2">
          <Button onClick={() => setConfirm(null)}>Not now</Button>
          <Button variant="primary" onClick={() => submit(true)} loading={busy}>Continue</Button>
        </div>
      </Modal>
    </div>
  );
}

function EngineOption({ active, onClick, title, subtitle, badge }: { active: boolean; onClick: () => void; title: string; subtitle: string; badge?: React.ReactNode }) {
  return (
    <button type="button" onClick={onClick}
      className={cn("rounded-lg border p-3 text-left transition-colors cursor-pointer", active ? "border-brand-500 bg-brand-500/10" : "border-base hover:bg-[var(--panel-2)]")}>
      <div className="flex items-center justify-between gap-2"><span className="text-sm font-medium">{title}</span>{badge}</div>
      <div className="mt-0.5 text-xs text-muted">{subtitle}</div>
    </button>
  );
}

function EstimateLine({ est, loading, error }: { est?: { est_cost_usd: number | null; est_wait_s: number | null; machine: { status: string; gpu: string; region_name: string } | null; message: string | null; model_name: string }; loading: boolean; error: unknown }) {
  if (error) return <div className="text-sm text-muted">Couldn't estimate the cost right now.</div>;
  if (!est) return <div className="text-sm text-muted">{loading ? "Estimating…" : ""}</div>;
  if (!est.machine) return <div className="text-sm text-amber-500">{est.message}</div>;
  const where = est.machine.status === "running" ? "on the machine that's already running"
    : est.machine.status === "starting" ? "on the machine that's starting up"
      : `a ${est.machine.gpu} will start in ${est.machine.region_name}`;
  return (
    <div className="flex items-center gap-2 text-sm">
      <Sparkles className="size-4 text-brand-500" />
      <span>
        <b>≈ {money(est.est_cost_usd)}</b> · ready in ~{duration(est.est_wait_s)} ·{" "}
        <span className="text-muted"><Cpu className="inline size-3.5" /> {where} · {est.model_name}</span>
      </span>
    </div>
  );
}
