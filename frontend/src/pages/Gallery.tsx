import { ArrowLeft, CheckSquare, FolderMinus, FolderPlus, Heart, HeartOff, Images, Pencil, Plus, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import {
  useBulk, useCollections, useCreateCollection, useDeleteCollection, useItems, useRenameCollection,
} from "@/api/queries";
import type { BulkAction, Collection } from "@/api/types";
import { AddToCollection } from "@/components/Collections";
import { ItemTile } from "@/components/ItemTile";
import { Button, Chips, Empty, ErrorNote, Input, Modal, PageHeader } from "@/components/ui";
import { cn } from "@/lib/format";

const KINDS = [
  { id: "all", label: "Everything" },
  { id: "image", label: "Images" },
  { id: "video", label: "Videos" },
  { id: "music", label: "Music" },
] as const;

const GRID = "grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5";

/** Your own gallery (only you see it), or one of your collections: endless scrolling, favorites, selecting. */
export function GalleryPage() {
  const { collectionId } = useParams();
  const [params, setParams] = useSearchParams();
  const kind = params.get("kind") ?? "all";
  const favorites = params.get("fav") === "1";
  const setFilter = (k: string, v: string | null) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v); else next.delete(k);
    setParams(next, { replace: true });
  };
  const cols = useCollections();
  const collection = cols.data?.find((c) => c.id === collectionId);
  const q = useItems({ kind, favorites, collection: collectionId });
  const items = useMemo(() => q.data?.pages.flatMap((p) => p.items) ?? [], [q.data]);

  const [selecting, setSelecting] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  useEffect(() => { setSelecting(false); setPicked(new Set()); }, [kind, favorites, collectionId]);
  const toggle = (id: string) => setPicked((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  // endless scrolling: the next page loads before the end comes into view
  const sentinel = useRef<HTMLDivElement>(null);
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = q;
  useEffect(() => {
    const el = sentinel.current;
    if (!el || !hasNextPage) return;
    const io = new IntersectionObserver((e) => {
      if (e[0]?.isIntersecting && !isFetchingNextPage) void fetchNextPage();
    }, { rootMargin: "1200px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, items.length]);

  const title = collectionId ? (collection?.name ?? "Collection") : "Gallery";
  const subtitle = collectionId
    ? collection ? `${collection.count} in this collection` : undefined
    : "Your own creations: only you can see them. Stored encrypted on your own storage.";
  return (
    <div className={cn(selecting && "pb-20")}>
      {collectionId && (
        <Link to="/gallery" className="mb-4 inline-flex items-center gap-1 text-sm text-muted hover:text-[var(--text)]">
          <ArrowLeft className="size-4" /> Gallery
        </Link>
      )}
      <PageHeader title={title} subtitle={subtitle} actions={
        <div className="flex gap-2">
          {collection && <CollectionActions c={collection} />}
          <Button size="sm" onClick={() => { setSelecting(!selecting); setPicked(new Set()); }} aria-pressed={selecting}>
            {selecting ? <><X className="size-4" /> Done</> : <><CheckSquare className="size-4" /> Select</>}
          </Button>
        </div>
      } />
      {!collectionId && <CollectionStrip cols={cols.data ?? []} />}
      <div className="mb-6 flex flex-wrap items-center gap-2">
        <Chips name="Show" value={kind} onChange={(v) => setFilter("kind", v && v !== "all" ? v : null)} options={KINDS.map((k) => ({ ...k }))} />
        <button type="button" role="switch" aria-checked={favorites} onClick={() => setFilter("fav", favorites ? null : "1")}
          className={cn("inline-flex h-9 cursor-pointer items-center gap-1.5 rounded-full border px-4 text-sm transition-colors",
            favorites ? "border-rose-500 bg-rose-500/15 text-rose-600 dark:text-rose-300" : "border-base hover:bg-[var(--panel-2)]")}>
          <Heart className={cn("size-4", favorites && "fill-current")} /> Favorites
        </button>
      </div>

      {q.error && <ErrorNote error={q.error} />}
      {q.isLoading && <div className={GRID}>{Array.from({ length: 10 }).map((_, i) => <div key={i} className="aspect-square rounded-xl skeleton" />)}</div>}
      {!q.isLoading && !q.error && items.length === 0 && (
        <Empty icon={favorites ? <Heart className="size-8" /> : <Images className="size-8" />}
          title={favorites ? "No favorites here yet" : collectionId ? "This collection is empty" : "Nothing here yet"}>
          {favorites ? "Tap the heart on a creation to keep it here."
            : collectionId ? "In the gallery, use Select to pick creations and add them to this collection."
            : "Your creations will show up here."}
        </Empty>
      )}
      <div className={GRID}>
        {items.map((i) => (
          <ItemTile key={i.id} item={i} selecting={selecting} selected={picked.has(i.id)} onToggle={() => toggle(i.id)} />
        ))}
        {isFetchingNextPage && Array.from({ length: 5 }).map((_, i) => <div key={`more-${i}`} className="aspect-square rounded-xl skeleton" />)}
      </div>
      <div ref={sentinel} aria-hidden className="h-px" />
      {!hasNextPage && items.length > 40 && <p className="mt-8 text-center text-xs text-muted">That's everything.</p>}

      {selecting && (
        <SelectionBar ids={[...picked]} collectionId={collectionId} onAll={() => setPicked(new Set(items.map((i) => i.id)))}
          onDone={() => setPicked(new Set())} />
      )}
    </div>
  );
}

function CollectionStrip({ cols }: { cols: Collection[] }) {
  const [making, setMaking] = useState(false);
  const make = useCreateCollection();
  const nav = useNavigate();
  const [name, setName] = useState("");
  return (
    <div className="mb-5">
      <div className="mb-2 text-xs uppercase tracking-wide text-muted">Collections</div>
      <div className="flex gap-3 overflow-x-auto pb-2">
        {cols.map((c) => (
          <Link key={c.id} to={`/gallery/c/${c.id}`} className="group w-32 shrink-0" data-testid="collection">
            <div className="aspect-square overflow-hidden rounded-xl panel-2">
              {c.cover_url
                ? <img src={c.cover_url} alt="" loading="lazy" className="h-full w-full object-cover transition-transform group-hover:scale-105" />
                : <div className="flex h-full items-center justify-center text-muted"><Images className="size-6" /></div>}
            </div>
            <div className="mt-1.5 truncate text-sm">{c.name}</div>
            <div className="text-xs text-muted">{c.count}</div>
          </Link>
        ))}
        <button type="button" onClick={() => setMaking(true)}
          className="flex aspect-square w-32 shrink-0 cursor-pointer flex-col items-center justify-center gap-1 rounded-xl border border-dashed border-base text-sm text-muted hover:bg-[var(--panel-2)]">
          <Plus className="size-5" /> New collection
        </button>
      </div>
      <Modal open={making} onClose={() => setMaking(false)} title="New collection">
        <form className="space-y-3" onSubmit={async (e) => {
          e.preventDefault();
          const c = await make.mutateAsync(name);
          setMaking(false);
          setName("");
          nav(`/gallery/c/${c.id}`);
        }}>
          <Input autoFocus aria-label="Name" placeholder="e.g. Wallpapers" maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
          <ErrorNote error={make.error} />
          <div className="flex justify-end"><Button type="submit" variant="primary" loading={make.isPending} disabled={!name.trim()}>Make</Button></div>
        </form>
      </Modal>
    </div>
  );
}

function CollectionActions({ c }: { c: Collection }) {
  const rename = useRenameCollection();
  const remove = useDeleteCollection();
  const nav = useNavigate();
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [name, setName] = useState(c.name);
  return (
    <>
      <Button size="sm" variant="ghost" onClick={() => { setName(c.name); setRenaming(true); }}><Pencil className="size-4" /> Rename</Button>
      <Button size="sm" variant="ghost" className="text-red-500" onClick={() => setDeleting(true)}><Trash2 className="size-4" /> Delete</Button>
      <Modal open={renaming} onClose={() => setRenaming(false)} title="Rename collection">
        <form className="space-y-3" onSubmit={async (e) => { e.preventDefault(); await rename.mutateAsync({ id: c.id, name }); setRenaming(false); }}>
          <Input autoFocus aria-label="Name" maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
          <ErrorNote error={rename.error} />
          <div className="flex justify-end"><Button type="submit" variant="primary" loading={rename.isPending} disabled={!name.trim()}>Save</Button></div>
        </form>
      </Modal>
      <Modal open={deleting} onClose={() => setDeleting(false)} title={`Delete “${c.name}”?`}>
        <p className="mb-6 text-sm text-muted">Only the collection goes. What's in it stays in your gallery.</p>
        <div className="flex justify-end gap-2">
          <Button onClick={() => setDeleting(false)}>Keep it</Button>
          <Button variant="danger" loading={remove.isPending} onClick={async () => { await remove.mutateAsync(c.id); nav("/gallery"); }}>Delete collection</Button>
        </div>
      </Modal>
    </>
  );
}

function SelectionBar({ ids, collectionId, onAll, onDone }: {
  ids: string[];
  collectionId?: string;
  onAll: () => void;
  onDone: () => void;
}) {
  const bulk = useBulk();
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const run = async (action: BulkAction, collection_id?: string) => {
    await bulk.mutateAsync({ ids, action, collection_id });
    onDone();
  };
  const none = ids.length === 0;
  return (
    <div className="fixed inset-x-0 bottom-16 z-40 flex justify-center px-4 md:bottom-6">
      <div className="panel flex flex-wrap items-center gap-2 rounded-xl p-2 shadow-2xl" role="toolbar" aria-label="Selected creations">
        <span className="px-2 text-sm font-medium tabular-nums">{ids.length} selected</span>
        <Button size="sm" variant="ghost" onClick={onAll}>All shown</Button>
        <Button size="sm" disabled={none} onClick={() => run("favorite")}><Heart className="size-4" /> Favorite</Button>
        <Button size="sm" disabled={none} onClick={() => run("unfavorite")}><HeartOff className="size-4" /> Unfavorite</Button>
        <Button size="sm" disabled={none} onClick={() => setAdding(true)}><FolderPlus className="size-4" /> Add to collection</Button>
        {collectionId && <Button size="sm" disabled={none} onClick={() => run("remove", collectionId)}><FolderMinus className="size-4" /> Take out</Button>}
        <Button size="sm" variant="ghost" className="text-red-500" disabled={none} onClick={() => setDeleting(true)}><Trash2 className="size-4" /> Delete</Button>
        {bulk.error && <ErrorNote error={bulk.error} />}
      </div>
      <AddToCollection open={adding} count={ids.length} onClose={() => setAdding(false)}
        onPick={async (cid) => { setAdding(false); await run("add", cid); }} />
      <Modal open={deleting} onClose={() => setDeleting(false)} title={`Delete ${ids.length} creation${ids.length === 1 ? "" : "s"}?`}>
        <p className="mb-6 text-sm text-muted">The files are erased from storage. This can't be undone.</p>
        <div className="flex justify-end gap-2">
          <Button onClick={() => setDeleting(false)}>Keep them</Button>
          <Button variant="danger" loading={bulk.isPending} onClick={async () => { await run("delete"); setDeleting(false); }}>Delete</Button>
        </div>
      </Modal>
    </div>
  );
}
