import { useEffect, useState, useCallback } from "react";
import { RefreshCw, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { adminApi, AdminOverview } from "@/lib/adminApi";
import { StatCard, SectionHeader } from "../AdminShell";

export function OverviewTab({
  onFrozenChange,
}: {
  onFrozenChange: (frozen: boolean) => void;
}) {
  const [data, setData] = useState<AdminOverview | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const d = await adminApi.overview();
      setData(d);
      onFrozenChange(Boolean(d?.frozen));
    } catch (err) {
      toast.error("Failed to load overview", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setLoading(false);
    }
  }, [onFrozenChange]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 30_000);
    return () => clearInterval(id);
  }, [refresh]);

  const n = (v: unknown) =>
    typeof v === "number" ? v.toLocaleString() : v == null ? "—" : String(v);

  return (
    <div>
      <SectionHeader
        title="Overview"
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

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatCard label="Total users" value={n(data?.total_users)} sub={`${n(data?.admins)} admins`} />
        <StatCard label="Active (24h)" value={n(data?.active_24h)} sub={`30d: ${n(data?.active_30d)}`} />
        <StatCard label="Suspended" value={n(data?.suspended)} accent={(data?.suspended ?? 0) > 0 ? "warning" : "default"} />
        <StatCard label="Conversations" value={n(data?.conversations)} />

        <StatCard label="Tasks (all time)" value={n(data?.tasks_total)} />
        <StatCard label="Tasks running" value={n(data?.tasks_running)} accent={(data?.tasks_running ?? 0) > 0 ? "success" : "default"} />
        <StatCard label="Tasks in 24h" value={n(data?.tasks_24h)} />
        <StatCard label="Failed (24h)" value={n(data?.tasks_failed_24h)} accent={(data?.tasks_failed_24h ?? 0) > 0 ? "danger" : "default"} />

        <StatCard label="Credits spent 7d" value={n(data?.credits_spent_7d)} />
        <StatCard label="Credits spent 30d" value={n(data?.credits_spent_30d)} />
        <StatCard
          label="System state"
          value={data?.frozen ? "FROZEN" : "LIVE"}
          accent={data?.frozen ? "danger" : "success"}
        />
        <StatCard label="Last refresh" value={new Date().toLocaleTimeString()} sub="auto every 30s" />
      </div>

      {data && (
        <details className="mt-6 rounded-lg border border-border bg-card p-4">
          <summary className="cursor-pointer text-sm font-medium">
            Raw payload
          </summary>
          <pre className="mt-3 max-h-80 overflow-auto rounded bg-muted p-3 text-xs">
            {JSON.stringify(data, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}
