import { AlertTriangle, CheckCircle2, Clock, Film, Image as ImageIcon, Loader2, Music, ScrollText, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { post } from "@/api/client";
import { useMe } from "@/api/queries";
import type { Job } from "@/api/types";
import { duration, isActive, money, stageLabel } from "@/lib/format";

import { LogViewer } from "./LogViewer";
import { Button, Card, Progress } from "./ui";

const STEPS = ["waiting_for_gpu", "loading_model", "generating", "saving"];

export const KindIcon = ({ kind, className }: { kind: string; className?: string }) =>
  kind === "video" ? <Film className={className} /> : kind === "music" ? <Music className={className} /> : <ImageIcon className={className} />;

export function JobCard({ job }: { job: Job }) {
  const active = isActive(job.state);
  const me = useMe();
  const [showLog, setShowLog] = useState(false);
  const stepIdx = STEPS.indexOf(job.stage);
  const overall =
    job.state === "succeeded" ? 1 : stepIdx < 0 ? 0.03 : (stepIdx + (job.stage === "generating" || job.stage === "loading_model" ? job.progress : 0.5)) / STEPS.length;
  return (
    <Card className="p-4">
      <div className="flex items-start gap-3">
        <div className="mt-0.5 rounded-lg panel-2 p-2"><KindIcon kind={job.kind} className="size-4" /></div>
        <div className="min-w-0 flex-1">
          <div className="line-clamp-2 text-sm">{job.prompt}</div>
          <div className="mt-1 text-xs text-muted">{job.model_name}{job.machine ? ` · ${job.machine}` : ""}</div>
        </div>
        {me.data?.role === "admin" && job.state !== "queued" && (
          <Button size="sm" variant="ghost" aria-label="Show log" title="The worker's log (admins)" onClick={() => setShowLog(true)}>
            <ScrollText className="size-4" />
          </Button>
        )}
        {active && (
          <Button size="sm" variant="ghost" aria-label="Cancel" onClick={() => post(`/jobs/${job.id}/cancel`)}>
            <X className="size-4" />
          </Button>
        )}
      </div>
      {showLog && <LogViewer jobId={job.id} label={`${job.model_name}: ${job.prompt.slice(0, 60)}`} onClose={() => setShowLog(false)} />}
      {active && (
        <div className="mt-3">
          <div className="mb-1.5 flex items-center justify-between text-xs">
            <span className="flex items-center gap-1.5 font-medium">
              {job.stage === "waiting_for_gpu" || job.stage === "waiting_in_line"
                ? <Clock className="size-3.5" />
                : <Loader2 className="size-3.5 animate-spin" />}
              {stageLabel(job.stage)}
              {job.stage === "generating" && job.progress > 0 ? ` ${Math.round(job.progress * 100)}%` : ""}
            </span>
            <span className="text-muted">
              {job.eta_s ? `about ${duration(job.eta_s)} left` : job.est_wait_s ? `usually ~${duration(job.est_wait_s)}` : ""}
            </span>
          </div>
          <Progress value={overall} />
          <ol className="mt-2 flex justify-between text-[11px] text-muted">
            {STEPS.map((s, i) => (
              <li key={s} className={i <= stepIdx ? "text-[var(--text)]" : ""}>{stageLabel(s)}</li>
            ))}
          </ol>
          {job.error && job.state === "waiting_capacity" && <div className="mt-2 text-xs text-amber-500">{job.error}</div>}
        </div>
      )}
      {job.state === "succeeded" && (
        <Link to={`/item/${job.id}`} className="mt-3 flex items-center gap-2 text-sm text-emerald-500 hover:underline">
          <CheckCircle2 className="size-4" /> Done{job.cost_usd ? ` · ${money(job.cost_usd)}` : ""} — view
        </Link>
      )}
      {job.state === "failed" && (
        <div className="mt-3 flex items-center justify-between gap-2 text-sm text-red-500">
          <span className="flex items-center gap-2"><AlertTriangle className="size-4" /> {job.error ?? "Didn't work"}</span>
          <Button size="sm" onClick={() => post(`/jobs/${job.id}/retry`)}>Try again</Button>
        </div>
      )}
      {job.state === "cancelled" && <div className="mt-3 text-sm text-muted">Cancelled</div>}
    </Card>
  );
}
