import { ArrowDownToLine, Copy, Download, Search, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useJobLog } from "@/api/queries";
import type { LogLine } from "@/api/types";
import { cn, isActive, stageLabel } from "@/lib/format";

const MAX_LINES = 20_000;
const MAX_RENDERED = 5_000;

const SOURCES: { id: string; label: string; color: string; hint: string }[] = [
  { id: "agent", label: "Worker", color: "text-emerald-300", hint: "aivo's worker on the GPU: model files, the job, delivering results" },
  { id: "engine", label: "AI engine", color: "text-amber-200", hint: "The AI engine's own output" },
];
const COLOR = Object.fromEntries(SOURCES.map((s) => [s.id, s.color]));
const LABEL = Object.fromEntries(SOURCES.map((s) => [s.id, s.label]));

const time = (ts: number) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour12: false }) : "--:--:--");
const asText = (l: LogLine) => `${time(l.ts)} [${LABEL[l.source] ?? l.source}] ${l.text}`;

/** The worker's log of one creation, for admins: model downloads, the job, the AI engine's output. */
export function LogViewer({ jobId, label, onClose }: { jobId: string; label: string; onClose: () => void }) {
  const [active, setActive] = useState(true);
  const log = useJobLog(jobId, active);
  const lines: LogLine[] = useMemo(() => (log.data?.lines ?? []).slice(-MAX_LINES), [log.data]);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [follow, setFollow] = useState(true);
  const [copied, setCopied] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const live = log.data ? isActive(log.data.state) : true;
  useEffect(() => setActive(live), [live]);
  const conn = log.isError
    ? { phase: "retrying", message: "Couldn't load the log, retrying…" }
    : !log.data
      ? { phase: "connecting", message: "Loading…" }
      : live
        ? { phase: "live", message: stageLabel(log.data.stage) }
        : { phase: "ended", message: log.data.state === "succeeded" ? "Finished" : log.data.error ?? "Ended" };

  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const l of lines) c[l.source] = (c[l.source] ?? 0) + 1;
    return c;
  }, [lines]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return lines.filter((l) => !hidden.has(l.source) && (!q || l.text.toLowerCase().includes(q)));
  }, [lines, hidden, query]);
  const rendered = visible.length > MAX_RENDERED ? visible.slice(visible.length - MAX_RENDERED) : visible;

  useEffect(() => {
    if (follow && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [rendered, follow]);

  const onScroll = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    setFollow(atBottom);
  }, []);

  const toggle = (id: string) =>
    setHidden((h) => {
      const n = new Set(h);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const text = () => visible.map(asText).join("\n") + "\n";
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text());
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable (e.g. plain http) */
    }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([text()], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `creation-${jobId.slice(0, 8)}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const dot =
    conn.phase === "live" ? "bg-emerald-400 animate-pulse"
      : conn.phase === "ended" ? "bg-zinc-500"
        : conn.phase === "connecting" ? "bg-zinc-400"
          : "bg-amber-400";

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/50" onClick={onClose}>
      <div
        role="dialog"
        aria-label="Creation log"
        className="flex h-full w-full max-w-5xl flex-col border-l border-zinc-800 bg-zinc-950 text-zinc-200 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-3 border-b border-zinc-800 px-4 py-3">
          <div className="min-w-0">
            <h2 className="font-semibold text-zinc-50">Creation log</h2>
            <div className="truncate text-xs text-zinc-400">{label}{log.data?.gpu_type ? ` · ${log.data.gpu_type}` : ""}</div>
          </div>
          <div className="flex items-center gap-3">
            <span className="flex items-center gap-2 text-xs text-zinc-300" data-testid="log-status" data-phase={conn.phase}>
              <span className={cn("size-2 rounded-full", dot)} /> {conn.message}
            </span>
            <button onClick={onClose} aria-label="Close" className="rounded p-1 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100">
              <X className="size-5" />
            </button>
          </div>
        </header>

        <div className="flex flex-wrap items-center gap-2 border-b border-zinc-800 px-4 py-2">
          <div className="flex flex-wrap gap-1" role="group" aria-label="Show lines from">
            {SOURCES.map((s) => (
              <button
                key={s.id}
                title={s.hint}
                aria-pressed={!hidden.has(s.id)}
                onClick={() => toggle(s.id)}
                className={cn(
                  "rounded-md border px-2 py-1 text-xs",
                  hidden.has(s.id) ? "border-zinc-800 text-zinc-500 line-through" : cn("border-zinc-700 bg-zinc-900", s.color),
                )}
              >
                {s.label} <span className="text-zinc-500">{counts[s.id] ?? 0}</span>
              </button>
            ))}
          </div>
          <label className="ml-auto flex h-8 items-center gap-1.5 rounded-md border border-zinc-700 bg-zinc-900 px-2 text-xs">
            <Search className="size-3.5 text-zinc-500" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search"
              aria-label="Search the log"
              className="w-40 bg-transparent text-zinc-100 outline-none placeholder:text-zinc-500"
            />
          </label>
          <button onClick={copy} className="flex h-8 items-center gap-1 rounded-md border border-zinc-700 px-2 text-xs hover:bg-zinc-800">
            <Copy className="size-3.5" /> {copied ? "Copied" : "Copy"}
          </button>
          <button onClick={download} className="flex h-8 items-center gap-1 rounded-md border border-zinc-700 px-2 text-xs hover:bg-zinc-800">
            <Download className="size-3.5" /> Download
          </button>
        </div>

        <div className="relative min-h-0 flex-1">
          <div
            ref={scroller}
            onScroll={onScroll}
            data-testid="log-lines"
            className="h-full overflow-auto px-4 py-2 font-mono text-[12px] leading-5"
          >
            {visible.length > rendered.length && (
              <div className="pb-2 text-zinc-500">Showing the last {MAX_RENDERED.toLocaleString()} lines. Download for everything.</div>
            )}
            {rendered.length === 0 && (
              <div className="py-8 text-center text-zinc-500">
                {lines.length === 0 ? (live ? "Waiting for a worker to report…" : "The worker sent no log lines.") : "No lines match."}
              </div>
            )}
            {rendered.map((l, i) => (
              <div
                key={i}
                className={cn(
                  "flex gap-3 whitespace-pre-wrap [overflow-wrap:anywhere]",
                  l.level === "error" && "bg-red-500/10 text-red-300",
                  l.level === "warning" && "text-amber-300",
                )}
              >
                <span className="shrink-0 select-none text-zinc-600">{time(l.ts)}</span>
                <span className={cn("w-[4.5rem] shrink-0 select-none truncate", COLOR[l.source] ?? "text-zinc-400")}>{LABEL[l.source] ?? l.source}</span>
                <span className="min-w-0">{l.text}</span>
              </div>
            ))}
          </div>
          {!follow && (
            <button
              onClick={() => setFollow(true)}
              className="absolute bottom-4 right-6 flex items-center gap-1.5 rounded-full bg-violet-600 px-3 py-1.5 text-xs font-medium text-white shadow-lg hover:bg-violet-500"
            >
              <ArrowDownToLine className="size-3.5" /> Jump to latest
            </button>
          )}
        </div>
        <footer className="border-t border-zinc-800 px-4 py-2 text-[11px] text-zinc-500">
          For troubleshooting. The worker's last 300 lines are kept with the creation; error messages from the AI engine can
          quote what was asked for.
        </footer>
      </div>
    </div>
  );
}
