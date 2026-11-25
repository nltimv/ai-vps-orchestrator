import { ArrowLeft, Clapperboard, Copy, Download, Pencil, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useDeleteJob, useJob } from "@/api/queries";
import { JobCard } from "@/components/JobCard";
import { MediaView } from "@/components/Media";
import { Badge, Button, Card, ErrorNote, Modal } from "@/components/ui";
import { ago, duration, isActive, money } from "@/lib/format";

import type { CreatePrefill } from "./Create";

export function ItemPage() {
  const { id } = useParams();
  const job = useJob(id);
  const nav = useNavigate();
  const del = useDeleteJob();
  const [idx, setIdx] = useState(0);
  const [confirmDelete, setConfirmDelete] = useState(false);
  if (job.isLoading) return <div className="h-96 rounded-xl skeleton" />;
  if (!job.data) return <ErrorNote error={job.error ?? "Not found"} />;
  const j = job.data;
  const out = j.outputs[Math.min(idx, j.outputs.length - 1)];
  const remix = (extra: CreatePrefill) =>
    nav("/", { state: { kind: j.kind, prompt: j.prompt, params: j.params, lyrics: j.lyrics, avoid: j.avoid,
      model_id: (j.params.model_id as string) ?? j.model_id, ...extra } satisfies CreatePrefill });

  return (
    <div>
      <Link to="/gallery" className="mb-4 inline-flex items-center gap-1 text-sm text-muted hover:text-[var(--text)]">
        <ArrowLeft className="size-4" /> Gallery
      </Link>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div>
          {/* while it's being made, what's ready already shows; the progress card says how far the rest is */}
          {isActive(j.state) && <div className="mb-3"><JobCard job={j} /></div>}
          {out ? <MediaView asset={out} /> : !isActive(j.state) && <JobCard job={j} />}
          {j.state === "succeeded" && j.error && <p className="mt-2 text-sm text-muted">{j.error}</p>}
          {j.outputs.length > 1 && (
            <div className="mt-3 flex flex-wrap gap-2">
              {j.outputs.map((o, i) => (
                <button key={o.id} onClick={() => setIdx(i)} className={`overflow-hidden rounded-lg border-2 ${i === idx ? "border-brand-500" : "border-transparent"}`}>
                  <img src={o.thumb_url ?? o.url} alt={`Version ${i + 1}`} className="size-16 object-cover" />
                </button>
              ))}
            </div>
          )}
        </div>
        <Card className="h-fit space-y-4 p-5">
          <div>
            <div className="text-xs uppercase tracking-wide text-muted">Description</div>
            <p className="mt-1 whitespace-pre-wrap text-sm">{j.prompt}</p>
          </div>
          {j.prompt_used && (
            <details className="group" data-testid="improved">
              <summary className="cursor-pointer text-xs uppercase tracking-wide text-muted hover:text-[var(--text)]">
                Improved description
              </summary>
              <p className="mt-1 whitespace-pre-wrap text-sm text-muted">{j.prompt_used}</p>
              <button type="button" className="mt-1 text-xs text-brand-500 hover:underline cursor-pointer"
                onClick={() => remix({ seed: null, prompt: j.prompt_used ?? j.prompt, params: { ...j.params, enhance: false } })}>
                Start from this text
              </button>
            </details>
          )}
          {j.lyrics && (
            <div>
              <div className="text-xs uppercase tracking-wide text-muted">Lyrics</div>
              <p className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap text-sm">{j.lyrics}</p>
            </div>
          )}
          <dl className="grid grid-cols-2 gap-y-2 text-sm">
            <dt className="text-muted">Engine</dt><dd className="flex items-center gap-1">{j.model_name}{j.adult && <Badge tone="bad" title="For adults only">18+</Badge>}</dd>
            <dt className="text-muted">Quality</dt><dd className="capitalize">{String(j.params.quality ?? "standard")}</dd>
            {out?.width && <><dt className="text-muted">Size</dt><dd>{out.width} × {out.height}</dd></>}
            {out?.duration_s && <><dt className="text-muted">Length</dt><dd>{duration(out.duration_s)}</dd></>}
            <dt className="text-muted">Made by</dt><dd>{j.user_name}</dd>
            <dt className="text-muted">When</dt><dd>{ago(j.created_at)}</dd>
            {j.machine && <><dt className="text-muted">GPU</dt><dd>{j.machine}</dd></>}
            <dt className="text-muted">Cost</dt><dd>{money(j.cost_usd ?? j.est_cost_usd)}</dd>
          </dl>
          {j.reference && (
            <div>
              <div className="mb-1 text-xs uppercase tracking-wide text-muted">Started from</div>
              <img src={j.reference.url} alt="Starting image" className="h-24 rounded-md object-cover" />
            </div>
          )}
          <div className="flex flex-wrap gap-2 border-t border-base pt-4">
            {out && <a href={out.download_url}><Button size="sm"><Download className="size-4" /> Download</Button></a>}
            <Button size="sm" onClick={() => remix({ seed: null })}><RefreshCw className="size-4" /> Variations</Button>
            {j.seed !== null && <Button size="sm" onClick={() => remix({ seed: j.seed })}><Copy className="size-4" /> Keep this look</Button>}
            {out && j.kind === "image" && (
              <>
                <Button size="sm" onClick={() => nav("/", { state: { kind: "video", prompt: "", reference: out,
                  ...(j.adult && j.mode === "t2i" ? { model_id: "wan22-i2v-adult" } : {}) } satisfies CreatePrefill })}>
                  <Clapperboard className="size-4" /> Animate this
                </Button>
                <Button size="sm" onClick={() => nav("/", { state: { kind: "image", prompt: "", reference: out } satisfies CreatePrefill })}>
                  <Pencil className="size-4" /> Edit this
                </Button>
              </>
            )}
            {!isActive(j.state) && (
              <Button size="sm" variant="ghost" className="text-red-500" onClick={() => setConfirmDelete(true)}>
                <Trash2 className="size-4" /> Delete
              </Button>
            )}
          </div>
          {j.state === "failed" && <Badge tone="bad">{j.error}</Badge>}
        </Card>
      </div>
      <Modal open={confirmDelete} onClose={() => setConfirmDelete(false)} title="Delete this creation?">
        <p className="mb-6 text-sm text-muted">The files are erased from storage. This can't be undone.</p>
        <div className="flex justify-end gap-2">
          <Button onClick={() => setConfirmDelete(false)}>Keep it</Button>
          <Button variant="danger" loading={del.isPending} onClick={async () => { await del.mutateAsync(j.id); nav("/gallery"); }}>Delete</Button>
        </div>
      </Modal>
    </div>
  );
}
