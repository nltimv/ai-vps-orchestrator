import { Check, FolderPlus, Images } from "lucide-react";
import { useState } from "react";

import { useCollections, useCreateCollection, useSetItemCollections } from "@/api/queries";
import type { Collection } from "@/api/types";
import { cn } from "@/lib/format";

import { Button, ErrorNote, Input, Modal } from "./ui";

/** Name a new collection; ``onMade`` gets it. */
function NewCollection({ onMade }: { onMade: (c: Collection) => void }) {
  const make = useCreateCollection();
  const [name, setName] = useState("");
  const submit = async () => {
    if (!name.trim()) return;
    const c = await make.mutateAsync(name);
    setName("");
    onMade(c);
  };
  return (
    <form className="mt-4 space-y-2" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <div className="flex gap-2">
        <Input aria-label="New collection" placeholder="New collection" maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
        <Button type="submit" loading={make.isPending} disabled={!name.trim()}><FolderPlus className="size-4" /> Make</Button>
      </div>
      <ErrorNote error={make.error} />
    </form>
  );
}

function Cover({ c }: { c: Collection }) {
  return c.cover_url
    ? <img src={c.cover_url} alt="" className="size-10 rounded-md object-cover" />
    : <div className="flex size-10 items-center justify-center rounded-md panel-2 text-muted"><Images className="size-4" /></div>;
}

/** Pick one collection to put the selected creations in (or make a new one). */
export function AddToCollection({ open, count, onClose, onPick }: {
  open: boolean;
  count: number;
  onClose: () => void;
  onPick: (id: string) => void;
}) {
  const cols = useCollections();
  return (
    <Modal open={open} onClose={onClose} title={`Add ${count} to a collection`}>
      <div className="max-h-80 space-y-1 overflow-y-auto">
        {(cols.data ?? []).map((c) => (
          <button key={c.id} type="button" onClick={() => onPick(c.id)}
            className="flex w-full cursor-pointer items-center gap-3 rounded-lg p-2 text-left hover:bg-[var(--panel-2)]">
            <Cover c={c} />
            <span className="flex-1 text-sm">{c.name}</span>
            <span className="text-xs text-muted">{c.count}</span>
          </button>
        ))}
        {cols.data?.length === 0 && <p className="text-sm text-muted">No collections yet. Make the first one:</p>}
      </div>
      <NewCollection onMade={(c) => onPick(c.id)} />
    </Modal>
  );
}

/** The collections one creation is in: tick to add or take out. */
export function ItemCollections({ open, itemId, inside, onClose }: {
  open: boolean;
  itemId: string;
  inside: string[];
  onClose: () => void;
}) {
  const cols = useCollections();
  const set = useSetItemCollections();
  const toggle = (id: string) =>
    set.mutate({ id: itemId, ids: inside.includes(id) ? inside.filter((x) => x !== id) : [...inside, id] });
  return (
    <Modal open={open} onClose={onClose} title="Collections">
      <div className="max-h-80 space-y-1 overflow-y-auto">
        {(cols.data ?? []).map((c) => {
          const on = inside.includes(c.id);
          return (
            <button key={c.id} type="button" role="checkbox" aria-checked={on} onClick={() => toggle(c.id)}
              className="flex w-full cursor-pointer items-center gap-3 rounded-lg p-2 text-left hover:bg-[var(--panel-2)]">
              <Cover c={c} />
              <span className="flex-1 text-sm">{c.name}</span>
              <span className={cn("flex size-5 items-center justify-center rounded border", on ? "border-brand-500 bg-brand-500 text-white" : "border-base")}>
                {on && <Check className="size-3.5" />}
              </span>
            </button>
          );
        })}
        {cols.data?.length === 0 && <p className="text-sm text-muted">No collections yet. Make the first one:</p>}
      </div>
      <ErrorNote error={set.error} />
      <NewCollection onMade={(c) => set.mutate({ id: itemId, ids: [...inside, c.id] })} />
    </Modal>
  );
}
