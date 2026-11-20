import { ArrowDownToLine, Copy, Download, Search, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Instance, LogLine, LogStatus } from "@/api/types";
import { cn } from "@/lib/format";

const MAX_LINES = 20_000;
const MAX_RENDERED = 5_000;

const SOURCES: { id: string; label: string; color: string; hint: string }[] = [
  { id: "aivo", label: "aivo", color: "text-violet-300", hint: "What aivo decided: which machine, retries, start-up steps" },
  { id: "setup", label: "Start-up", color: "text-sky-300", hint: "The machine's start-up script" },
  { id: "agent", label: "Agent", color: "text-emerald-300", hint: "aivo's helper program on the machine" },
  { id: "engine", label: "AI engine", color: "text-amber-200", hint: "The AI engine's own output" },
];
const COLOR = Object.fromEntries(SOURCES.map((s) => [s.id, s.color]));
const LABEL = Object.fromEntries(SOURCES.map((s) => [s.id, s.label]));

const time = (ts: number) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour12: false }) : "--:--:--");
const asText = (l: LogLine) => `${time(l.ts)} [${LABEL[l.source] ?? l.source}] ${l.text}`;

type Conn = { phase: LogStatus["phase"] | "connecting" | "retrying"; message: string };

/** Live machine log for admins: aivo's decisions, the start-up script, the agent and the AI engine. */
export function LogViewer({ machine, onClose }: { machine: Instance; onClose: () => void }) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [conn, setConn] = useState<Conn>({ phase: "connecting", message: "Connecting…" });
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [follow, setFollow] = useState(true);
  const [copied, setCopied] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const es = new EventSource(`/api/v1/machines/${machine.id}/logs`, { withCredentials: true });
    es.addEventListener("lines", (ev) => {
      const batch: LogLine[] = JSON.parse((ev as MessageEvent).data).lines ?? [];
      if (!batch.length) return;
      setLines((old) => {
        let next = old.concat(batch);
        // aivo's own lines and the machine's lines arrive in separate batches: keep one timeline
        const minTs = Math.min(...batch.map((l) => l.ts || Infinity));
        if (old.length && minTs < (old[old.length - 1].ts || 0)) next = next.sort((a, b) => (a.ts || 0) - (b.ts || 0));
        return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
      });
    });
    es.addEventListener("status", (ev) => {
      const s: LogStatus = JSON.parse((ev as MessageEvent).data);
      setConn({ phase: s.phase, message: s.message });
    });
    es.addEventListener("end", () => {
      es.close(); // don't reconnect: the machine is gone
      setConn({ phase: "ended", message: "Machine shut down — showing the saved log" });
    });
    es.onerror = () => {
      if (es.readyState !== EventSource.CLOSED) setConn({ phase: "retrying", message: "Connection lost, reconnecting…" });
    };
    return () => es.close();
  }, [machine.id]);

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
    a.download = `machine-${machine.name}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.txt`;
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
        aria-label="Machine log"
        className="flex h-full w-full max-w-5xl flex-col border-l border-zinc-800 bg-zinc-950 text-zinc-200 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-3 border-b border-zinc-800 px-4 py-3">
          <div className="min-w-0">
            <h2 className="font-semibold text-zinc-50">Machine log</h2>
            <div className="truncate text-xs text-zinc-400">{machine.gpu} · {machine.region_name} · {machine.name}</div>
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
                {lines.length === 0 ? "Waiting for the first lines…" : "No lines match."}
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
          For troubleshooting. Error messages from the AI engine can quote what was asked for; the log isn't stored, except the
          last lines of a machine after it shuts down.
        </footer>
      </div>
    </div>
  );
}
