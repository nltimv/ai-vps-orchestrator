import { Film, Music, Play } from "lucide-react";

import type { Asset, Job } from "@/api/types";

/** Small preview used in grids. */
export function Thumb({ job }: { job: Job }) {
  const out = job.outputs[0];
  if (!out) return <div className="aspect-square skeleton" />;
  const src = out.thumb_url ?? (out.media_type.startsWith("image/") ? out.url : null);
  return (
    <div className="relative aspect-square overflow-hidden bg-black/40">
      {src ? (
        <img src={src} alt="" loading="lazy" className={job.kind === "music" ? "h-full w-full object-contain p-6" : "h-full w-full object-cover"} />
      ) : (
        <div className="flex h-full items-center justify-center text-muted">{job.kind === "video" ? <Film /> : <Music />}</div>
      )}
      {job.kind !== "image" && (
        <div className="absolute bottom-2 left-2 flex items-center gap-1 rounded-full bg-black/60 px-2 py-0.5 text-xs text-white">
          {job.kind === "video" ? <Play className="size-3" /> : <Music className="size-3" />}
          {out.duration_s ? `${Math.round(out.duration_s)} s` : ""}
        </div>
      )}
      {job.outputs.length > 1 && (
        <div className="absolute right-2 top-2 rounded-full bg-black/60 px-2 py-0.5 text-xs text-white">{job.outputs.length}</div>
      )}
    </div>
  );
}

/** Full viewer: images, seekable video (HTTP Range on the encrypted store), audio with waveform. */
export function MediaView({ asset }: { asset: Asset }) {
  if (asset.media_type.startsWith("image/")) {
    return <img src={asset.url} alt="" className="max-h-[75vh] w-full rounded-lg object-contain" />;
  }
  if (asset.media_type.startsWith("video/")) {
    return (
      <video src={asset.url} poster={asset.thumb_url ?? undefined} controls playsInline preload="metadata"
        className="max-h-[75vh] w-full rounded-lg bg-black" />
    );
  }
  return (
    <div className="rounded-lg panel-2 p-4">
      {asset.thumb_url && <img src={asset.thumb_url} alt="" className="mb-3 h-32 w-full object-fill opacity-80" />}
      <audio src={asset.url} controls preload="metadata" className="w-full" />
    </div>
  );
}
