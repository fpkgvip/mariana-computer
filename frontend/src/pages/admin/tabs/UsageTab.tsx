import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { adminApi, UsageRollupRow } from "@/lib/adminApi";
import { StatCard, SectionHeader } from "../AdminShell";

export function UsageTab() {
  const [rows, setRows] = useState<UsageRollupRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(30);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await adminApi.usageRollup(days);
      setRows(Array.isArray(data) ? data : []);
    } catch (err) {
      toast.error("Failed to load usage", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const totals = useMemo(() => {
    let credits = 0;
    let cost = 0;
    let tasks = 0;
    let failed = 0;
    const perDay = new Map<string, { credits: number; tasks: number }>();
    const perUser = new Map<string, { credits: number; tasks: number }>();
    for (const r of rows) {
      credits += Number(r.credits_spent ?? 0);
      cost += Number(r.cost_usd ?? 0);
      tasks += Number(r.tasks_total ?? 0);
      failed += Number(r.tasks_failed ?? 0);
      const d = String(r.day ?? "");
      if (d) {
        const prev = perDay.get(d) ?? { credits: 0, tasks: 0 };
        perDay.set(d, {
          credits: prev.credits + Number(r.credits_spent ?? 0),
          tasks: prev.tasks + Number(r.tasks_total ?? 0),
        });
      }
      const u = String(r.user_id ?? "unknown");
      const prevU = perUser.get(u) ?? { credits: 0, tasks: 0 };
      perUser.set(u, {
        credits: prevU.credits + Number(r.credits_spent ?? 0),
        tasks: prevU.tasks + Number(r.tasks_total ?? 0),
      });
    }
    const topUsers = [...perUser.entries()]
      .sort((a, b) => b[1].credits - a[1].credits)
      .slice(0, 10);
    const sortedDays = [...perDay.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1));
    return { credits, cost, tasks, failed, topUsers, sortedDays };
  }, [rows]);

  return (
    <div>
      <SectionHeader
        title="Usage & Costs"
        action={
          <div className="flex gap-2">
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              className="rounded-md border border-border bg-background px-3 py-1.5 text-sm"
            >
              {[7, 14, 30, 60, 90, 180, 365].map((d) => (
                <option key={d} value={d}>
                  Last {d}d
                </option>
              ))}
            </select>
            <button
              onClick={refresh}
              disabled={loading}
              className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
            >
              {loading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="h-4 w-4" />
              )}
              Refresh
            </button>
          </div>
        }
      />

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatCard label="Tasks" value={totals.tasks.toLocaleString()} sub={`${days}-day window`} />
        <StatCard
          label="Failed"
          value={totals.failed.toLocaleString()}
          accent={totals.failed > 0 ? "danger" : "default"}
        />
        <StatCard
          label="Credits spent"
          value={totals.credits.toLocaleString()}
        />
        <StatCard label="Cost (USD)" value={`$${totals.cost.toFixed(2)}`} />
      </div>

      {!loading && rows.length === 0 && (
        <p className="mt-6 rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground">
          No usage rows yet. The <code className="font-mono">usage_rollup_daily</code>{" "}
          table starts populating after tasks are recorded.
        </p>
      )}

      {totals.topUsers.length > 0 && (
        <div className="mt-8">
          <h3 className="font-serif text-base font-semibold">Top 10 by spend</h3>
          <div className="mt-2 overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 text-left">
                <tr>
                  <th className="px-3 py-2">User</th>
                  <th className="px-3 py-2">Credits</th>
                  <th className="px-3 py-2">Tasks</th>
                </tr>
              </thead>
              <tbody>
                {totals.topUsers.map(([u, v]) => (
                  <tr key={u} className="border-t border-border">
                    <td className="px-3 py-2 font-mono text-xs">{u}</td>
                    <td className="px-3 py-2 font-mono">{v.credits.toLocaleString()}</td>
                    <td className="px-3 py-2 font-mono">{v.tasks.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {totals.sortedDays.length > 0 && (
        <div className="mt-8">
          <h3 className="font-serif text-base font-semibold">Daily breakdown</h3>
          <div className="mt-2 overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 text-left">
                <tr>
                  <th className="px-3 py-2">Day</th>
                  <th className="px-3 py-2">Tasks</th>
                  <th className="px-3 py-2">Credits</th>
                </tr>
              </thead>
              <tbody>
                {totals.sortedDays.map(([day, v]) => (
                  <tr key={day} className="border-t border-border">
                    <td className="px-3 py-2 font-mono">{day}</td>
                    <td className="px-3 py-2 font-mono">{v.tasks.toLocaleString()}</td>
                    <td className="px-3 py-2 font-mono">{v.credits.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
