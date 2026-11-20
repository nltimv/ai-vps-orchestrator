import { Images } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { useGallery } from "@/api/queries";
import { Thumb } from "@/components/Media";
import { Button, Chips, Empty, PageHeader, Toggle } from "@/components/ui";
import { ago } from "@/lib/format";

const KINDS = [
  { id: "all", label: "Everything" },
  { id: "image", label: "Images" },
  { id: "video", label: "Videos" },
  { id: "music", label: "Music" },
] as const;

export function GalleryPage() {
  const [kind, setKind] = useState<string>("all");
  const [mine, setMine] = useState(false);
  const q = useGallery(kind, mine);
  const items = q.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <div>
      <PageHeader title="Gallery" subtitle="Everything created here. Stored encrypted on your own storage."
        actions={<Toggle checked={mine} onChange={setMine} label="Only mine" />} />
      <div className="mb-6"><Chips name="Filter" value={kind} onChange={(v) => setKind(v ?? "all")} options={KINDS.map((k) => ({ ...k }))} /></div>
      {q.isLoading && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          {Array.from({ length: 8 }).map((_, i) => <div key={i} className="aspect-square rounded-xl skeleton" />)}
        </div>
      )}
      {!q.isLoading && items.length === 0 && (
        <Empty icon={<Images className="size-8" />} title="Nothing here yet">Your creations will show up here.</Empty>
      )}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
        {items.map((j) => (
          <Link key={j.id} to={`/item/${j.id}`} className="group panel overflow-hidden rounded-xl transition-transform hover:-translate-y-0.5">
            <Thumb job={j} />
            <div className="p-3">
              <div className="line-clamp-2 text-sm">{j.prompt}</div>
              <div className="mt-1 text-xs text-muted">{j.user_name} · {ago(j.created_at)}</div>
            </div>
          </Link>
        ))}
      </div>
      {q.hasNextPage && (
        <div className="mt-8 flex justify-center">
          <Button onClick={() => q.fetchNextPage()} loading={q.isFetchingNextPage}>Show more</Button>
        </div>
      )}
    </div>
  );
}
