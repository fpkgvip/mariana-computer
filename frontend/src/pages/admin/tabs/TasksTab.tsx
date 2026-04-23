import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { adminApi, AdminTaskRow } from "@/lib/adminApi";
import { SectionHeader } from "../AdminShell";

const STATUS_OPTIONS = ["", "PENDING", "RUNNING", "COMPLETED", "FAILED", "HALTED", "CANCELLED"];

export function TasksTab() {
  const [rows, setRows] = useState<AdminTaskRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState("");
  const [userId, setUserId] = useState("");
  const [limit] = useState(100);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await adminApi.listTasks({
        status: status || undefined,
        user_id: userId.trim() || undefined,
        limit,
      });
      setRows(Array.isArray(data) ? data : []);
    } catch (err) {
      toast.error("Failed to load tasks", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setLoading(false);
    }
  }, [status, userId, limit]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const statusColor = (s: string | null) => {
    switch (s) {
      case "RUNNING":
        return "bg-emerald-500/15 text-emerald-500";
      case "COMPLETED":
        return "bg-primary/15 text-primary";
      case "FAILED":
      case "HALTED":
      case "CANCELLED":
        return "bg-destructive/15 text-destructive";
      default:
        return "bg-muted text-muted-foreground";
    }
  };

  return (
    <div>
      <SectionHeader
        title={`User tasks (${rows.length})`}
        action={
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
        }
      />

      <div className="mb-4 flex flex-wrap gap-2">
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-2 text-sm"
        >
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s || "All statuses"}
            </option>
          ))}
        </select>
        <input
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
          placeholder="Filter by user UUID…"
          className="w-[320px] rounded-md border border-border bg-background px-3 py-2 text-sm"
        />
      </div>

      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-muted/40 text-left">
            <tr>
              <th className="px-3 py-2">Topic</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Tier</th>
              <th className="px-3 py-2">Budget ($)</th>
              <th className="px-3 py-2">User</th>
              <th className="px-3 py-2">Created</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-muted-foreground">
                  <Loader2 className="mx-auto h-4 w-4 animate-spin" />
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-muted-foreground">
                  No tasks match.
                </td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.task_id} className="border-t border-border">
                <td className="px-3 py-2">
                  <div className="truncate max-w-[360px]" title={r.topic ?? ""}>
                    {r.topic ?? "—"}
                  </div>
                  <div className="font-mono text-xs text-muted-foreground">
                    {r.task_id.slice(0, 8)}…
                  </div>
                </td>
                <td className="px-3 py-2">
                  <span className={`rounded-md px-2 py-0.5 text-xs ${statusColor(r.status)}`}>
                    {r.status ?? "—"}
                  </span>
                </td>
                <td className="px-3 py-2">{r.tier ?? "—"}</td>
                <td className="px-3 py-2 font-mono">
                  {r.budget_usd == null ? "—" : `$${Number(r.budget_usd).toFixed(2)}`}
                </td>
                <td className="px-3 py-2 font-mono text-xs">
                  {r.user_id?.slice(0, 8) ?? "—"}…
                </td>
                <td className="px-3 py-2 text-xs text-muted-foreground">
                  {r.created_at ? new Date(r.created_at).toLocaleString() : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
