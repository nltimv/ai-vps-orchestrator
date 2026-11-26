import { ArrowLeft, Clapperboard, Copy, Download, FolderPlus, Heart, Pencil, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";

import { ApiError } from "@/api/client";
import { useCollections, useDeleteItem, useFavorite, useItem, useJob } from "@/api/queries";
import type { ItemDetail } from "@/api/types";
import { ItemCollections } from "@/components/Collections";
import { JobCard } from "@/components/JobCard";
import { MediaView } from "@/components/Media";
import { Badge, Button, Card, ErrorNote, Modal } from "@/components/ui";
import { ago, cn, duration, isActive, money } from "@/lib/format";

import type { CreatePrefill } from "./Create";

const NOUN = { image: "picture", video: "video", music: "song" } as const;

/** One creation: a single picture, video or song, with the others made together with it alongside. */
export function ItemPage() {
  const { id } = useParams();
  const q = useItem(id);
  // an older link may name the whole run: show its first result, or its progress while nothing is ready
  const missing = q.error instanceof ApiError && q.error.status === 404;
  const job = useJob(missing ? id : undefined);
  if (q.isLoading || (missing && job.isLoading)) return <div className="h-96 rounded-xl skeleton" />;
  if (missing && job.data) {
    const first = job.data.outputs[0];
    if (first) return <Navigate to={`/item/${first.id}`} replace />;
    return <div className="max-w-xl"><JobCard job={job.data} /></div>;
  }
  if (!q.data) return <ErrorNote error={missing ? "Not found" : (q.error ?? "Not found")} />;
  return <ItemView d={q.data} />;
}

function ItemView({ d }: { d: ItemDetail }) {
  const { item, job: j, improved, seed } = d;
  const nav = useNavigate();
  const fav = useFavorite();
  const del = useDeleteItem();
  const cols = useCollections();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [picking, setPicking] = useState(false);
  const out = item.media;
  const noun = NOUN[item.kind] ?? "creation";
  const others = j.outputs;
  const remix = (extra: CreatePrefill) =>
    nav("/", { state: { kind: j.kind, prompt: j.prompt, params: j.params, lyrics: j.lyrics, avoid: j.avoid,
      model_id: (j.params.model_id as string) ?? j.model_id, ...extra } satisfies CreatePrefill });
  // made in a run of its own (its own description and look number): the same text and number make it again
  const ownRun = j.prompts_used.length > 1 && !!improved;
  const keepLook = () => remix(ownRun
    ? { seed, prompt: improved ?? j.prompt, params: { ...j.params, enhance: false, count: 1 } }
    : { seed });
  const inCollections = (cols.data ?? []).filter((c) => d.collection_ids.includes(c.id));
  const afterDelete = () => {
    const next = others.find((o) => o.id !== item.id);
    nav(next ? `/item/${next.id}` : "/gallery", { replace: true });
  };

  return (
    <div>
      <Link to="/gallery" className="mb-4 inline-flex items-center gap-1 text-sm text-muted hover:text-[var(--text)]">
        <ArrowLeft className="size-4" /> Gallery
      </Link>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div>
          {isActive(j.state) && <div className="mb-3"><JobCard job={j} /></div>}
          <MediaView asset={out} />
          {others.length > 1 && (
            <div className="mt-4">
              <div className="mb-2 text-xs uppercase tracking-wide text-muted">Made together</div>
              <div className="flex flex-wrap gap-2">
                {others.map((o, i) => (
                  <Link key={o.id} to={`/item/${o.id}`} replace aria-current={o.id === item.id ? "page" : undefined}
                    className={cn("overflow-hidden rounded-lg border-2", o.id === item.id ? "border-brand-500" : "border-transparent hover:border-[var(--border)]")}>
                    <img src={o.thumb_url ?? o.url} alt={`Made together ${i + 1}`} className="size-16 object-cover" />
                  </Link>
                ))}
              </div>
            </div>
          )}
        </div>
        <Card className="h-fit space-y-4 p-5">
          <div className="flex flex-wrap gap-2">
            <Button size="sm" aria-pressed={item.favorite} onClick={() => fav.mutate({ id: item.id, favorite: !item.favorite })}>
              <Heart className={cn("size-4", item.favorite && "fill-rose-500 text-rose-500")} />
              {item.favorite ? "Favorite" : "Add to favorites"}
            </Button>
            <Button size="sm" onClick={() => setPicking(true)}>
              <FolderPlus className="size-4" />
              {inCollections.length ? `In ${inCollections.length} collection${inCollections.length === 1 ? "" : "s"}` : "Add to collection"}
            </Button>
          </div>
          {inCollections.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {inCollections.map((c) => <Link key={c.id} to={`/gallery/c/${c.id}`}><Badge tone="brand">{c.name}</Badge></Link>)}
            </div>
          )}
          <div>
            <div className="text-xs uppercase tracking-wide text-muted">Description</div>
            <p className="mt-1 whitespace-pre-wrap text-sm">{j.prompt}</p>
          </div>
          {improved && (
            <details className="group" data-testid="improved">
              <summary className="cursor-pointer text-xs uppercase tracking-wide text-muted hover:text-[var(--text)]">
                Improved description
              </summary>
              <p className="mt-1 whitespace-pre-wrap text-sm text-muted">{improved}</p>
              <button type="button" className="mt-1 text-xs text-brand-500 hover:underline cursor-pointer"
                onClick={() => remix({ seed: null, prompt: improved, params: { ...j.params, enhance: false } })}>
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
            {out.width && <><dt className="text-muted">Size</dt><dd>{out.width} × {out.height}</dd></>}
            {out.duration_s && <><dt className="text-muted">Length</dt><dd>{duration(out.duration_s)}</dd></>}
            <dt className="text-muted">When</dt><dd>{ago(item.created_at)}</dd>
            {j.machine && <><dt className="text-muted">GPU</dt><dd>{j.machine}</dd></>}
            <dt className="text-muted">Cost</dt>
            <dd>{money(j.cost_usd ?? j.est_cost_usd)}{(j.expected ?? others.length) > 1 && <span className="text-muted"> for all made together</span>}</dd>
          </dl>
          {j.reference && (
            <div>
              <div className="mb-1 text-xs uppercase tracking-wide text-muted">Started from</div>
              <img src={j.reference.url} alt="Starting image" className="h-24 rounded-md object-cover" />
            </div>
          )}
          <div className="flex flex-wrap gap-2 border-t border-base pt-4">
            <a href={out.download_url}><Button size="sm"><Download className="size-4" /> Download</Button></a>
            <Button size="sm" onClick={() => remix({ seed: null })}><RefreshCw className="size-4" /> Variations</Button>
            {seed !== null && <Button size="sm" onClick={keepLook}><Copy className="size-4" /> Keep this look</Button>}
            {j.kind === "image" && (
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
            <Button size="sm" variant="ghost" className="text-red-500" onClick={() => setConfirmDelete(true)}>
              <Trash2 className="size-4" /> Delete
            </Button>
          </div>
        </Card>
      </div>
      <ItemCollections open={picking} itemId={item.id} inside={d.collection_ids} onClose={() => setPicking(false)} />
      <Modal open={confirmDelete} onClose={() => setConfirmDelete(false)} title={`Delete this ${noun}?`}>
        <p className="mb-6 text-sm text-muted">
          The file is erased from storage. This can't be undone.
          {others.length > 1 && " The others made together with it stay."}
        </p>
        <div className="flex justify-end gap-2">
          <Button onClick={() => setConfirmDelete(false)}>Keep it</Button>
          <Button variant="danger" loading={del.isPending} onClick={async () => { await del.mutateAsync(item.id); afterDelete(); }}>Delete</Button>
        </div>
      </Modal>
    </div>
  );
}
