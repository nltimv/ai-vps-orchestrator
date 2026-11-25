import { useSpending } from "@/api/queries";
import { Badge, Card, PageHeader, Progress, Stat } from "@/components/ui";
import { ago, money } from "@/lib/format";

export function SpendingPage() {
  const q = useSpending();
  if (!q.data) return <div className="h-64 rounded-xl skeleton" />;
  const s = q.data;
  const used = s.compute_usd + s.reserved_usd;
  const maxDay = Math.max(0.01, ...s.by_day.map((d) => d.usd));
  return (
    <div>
      <PageHeader title="Spending" subtitle={`This month (since ${new Date(s.period).toLocaleDateString(undefined, { month: "long", day: "numeric" })})`} />
      <Card className="mb-6 p-5">
        <div className="mb-2 flex items-center justify-between text-sm">
          <span>{money(s.compute_usd, 2)} spent on GPUs {s.reserved_usd > 0 && <span className="text-muted">(+{money(s.reserved_usd, 2)} set aside for running work)</span>}</span>
          <span className="text-muted">limit {money(s.hard_cap_usd, 0)}</span>
        </div>
        <Progress value={used / Math.max(s.hard_cap_usd, 0.01)} />
        <div className="mt-2 text-xs text-muted">
          You'll be asked to confirm new creations above {money(s.soft_cap_usd, 0)}. {s.locked && <Badge tone="bad">Limit reached — new creations are paused</Badge>}
        </div>
      </Card>
      <div className="mb-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="GPUs" value={money(s.compute_usd, 2)} sub="this month" />
        <Stat label="Model cache" value={money(s.storage_monthly_usd, 2)} sub="per month (estimate)" />
        <Stat label="Running now" value={s.running_machines} sub={s.running_machines ? "GPUs" : "nothing is billing"} />
        <Stat label="Cloud bill so far" value={money(s.provider_month_to_date_usd, 2)} sub="as reported by the provider (whole account)" />
      </div>
      {s.by_day.length > 0 && (
        <Card className="mb-8 p-5">
          <div className="mb-4 text-sm font-medium">Per day</div>
          <div className="flex h-40 items-end gap-1">
            {s.by_day.map((d) => (
              <div key={d.day} className="group flex flex-1 flex-col items-center gap-1">
                <div className="w-full rounded-t brand-gradient" style={{ height: `${Math.max(2, (d.usd / maxDay) * 100)}%` }} title={`${d.day}: ${money(d.usd)}`} />
                <div className="text-[10px] text-muted">{d.day.slice(8)}</div>
              </div>
            ))}
          </div>
        </Card>
      )}
      <div className="grid gap-6 lg:grid-cols-2">
        <Card className="p-5">
          <div className="mb-3 text-sm font-medium">Recent creations</div>
          <table className="w-full text-sm">
            <tbody className="divide-y divide-[var(--border)]">
              {s.recent_jobs.map((j) => (
                <tr key={j.id}><td className="py-2">{j.model}</td><td className="text-muted">{j.user}</td>
                  <td className="text-muted">{ago(j.created_at)}</td><td className="text-right">{money(j.cost_usd ?? j.est_cost_usd)}</td></tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card className="p-5">
          <div className="mb-3 text-sm font-medium">GPUs</div>
          <table className="w-full text-sm">
            <tbody className="divide-y divide-[var(--border)]">
              {s.by_machine.map((m) => (
                <tr key={m.id}><td className="py-2">{m.gpu}</td><td className="text-muted">{m.region}</td>
                  <td className="text-muted">{m.hours.toFixed(2)} h</td><td className="text-right">{money(m.usd)}</td></tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
    </div>
  );
}
