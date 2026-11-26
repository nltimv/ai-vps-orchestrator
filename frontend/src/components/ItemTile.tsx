import { Check, Film, Heart, Music, Play } from "lucide-react";
import { Link } from "react-router-dom";

import { useFavorite } from "@/api/queries";
import type { Item } from "@/api/types";
import { cn } from "@/lib/format";

/** One creation in the gallery grid: opens its page, or is picked when selecting. The heart works either way. */
export function ItemTile({ item, selecting, selected, onToggle }: {
  item: Item;
  selecting: boolean;
  selected: boolean;
  onToggle: () => void;
}) {
  const fav = useFavorite();
  const m = item.media;
  const src = m.thumb_url ?? (m.media_type.startsWith("image/") ? m.url : null);
  const tile = (
    <div className={cn("relative aspect-square overflow-hidden rounded-xl bg-black/40 transition", selected && "ring-4 ring-brand-500")}>
      {src ? (
        <img src={src} alt={item.prompt} loading="lazy"
          className={item.kind === "music" ? "h-full w-full object-contain p-6" : "h-full w-full object-cover"} />
      ) : (
        <div className="flex h-full items-center justify-center text-muted">{item.kind === "video" ? <Film /> : <Music />}</div>
      )}
      {item.kind !== "image" && (
        <div className="absolute bottom-2 left-2 flex items-center gap-1 rounded-full bg-black/60 px-2 py-0.5 text-xs text-white">
          {item.kind === "video" ? <Play className="size-3" /> : <Music className="size-3" />}
          {m.duration_s ? `${Math.round(m.duration_s)} s` : ""}
        </div>
      )}
      {item.adult && <span className="absolute bottom-2 right-2 rounded bg-red-600 px-1.5 py-0.5 text-[10px] font-semibold text-white">18+</span>}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 to-transparent p-3 pt-10 text-xs text-white opacity-0 transition-opacity group-hover:opacity-100">
        <p className="line-clamp-2">{item.prompt}</p>
      </div>
      {selecting && (
        <span className={cn("absolute left-2 top-2 flex size-6 items-center justify-center rounded-full border-2 border-white shadow",
          selected ? "bg-brand-500" : "bg-black/30")}>
          {selected && <Check className="size-4 text-white" />}
        </span>
      )}
    </div>
  );
  return (
    <div className="group relative" data-testid="item">
      {selecting ? (
        <button type="button" onClick={onToggle} aria-pressed={selected} aria-label={`Select: ${item.prompt.slice(0, 60)}`}
          className="block w-full cursor-pointer text-left">{tile}</button>
      ) : (
        <Link to={`/item/${item.id}`} className="block transition-transform hover:-translate-y-0.5">{tile}</Link>
      )}
      <button type="button" aria-label={item.favorite ? "Remove from favorites" : "Add to favorites"} aria-pressed={item.favorite}
        onClick={() => fav.mutate({ id: item.id, favorite: !item.favorite })}
        className={cn("absolute right-2 top-2 rounded-full bg-black/40 p-1.5 text-white transition-opacity hover:bg-black/60 cursor-pointer",
          // no hover on touch screens: the heart is always there
          item.favorite ? "opacity-100" : "opacity-0 focus:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100")}>
        <Heart className={cn("size-4", item.favorite && "fill-rose-500 text-rose-500")} />
      </button>
    </div>
  );
}
