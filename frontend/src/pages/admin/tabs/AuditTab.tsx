import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { adminApi, AuditEntry } from "@/lib/adminApi";
import { SectionHeader } from "../AdminShell";

export function AuditTab() {
  const [rows, setRows] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionFilter, setActionFilter] = useState("");
  const [limit] = useState(200);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await adminApi.auditLog({
        limit,
        action: actionFilter.trim() || undefined,
      });
      setRows(Array.isArray(data) ? data : []);
    } catch (err) {
      toast.error("Failed to load audit log", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setLoading(false);
    }
  }, [actionFilter, limit]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <div>
      <SectionHeader
        title={`Audit log (${rows.length})`}
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

      <div className="mb-4">
        <input
          value={actionFilter}
          onChange={(e) => setActionFilter(e.target.value)}
          placeholder="Filter by action (e.g. user.role.set, credits.adjust)…"
          className="w-[420px] max-w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
        />
      </div>

      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-muted/40 text-left">
            <tr>
              <th className="px-3 py-2">When</th>
              <th className="px-3 py-2">Actor</th>
              <th className="px-3 py-2">Action</th>
              <th className="px-3 py-2">Target</th>
              <th className="px-3 py-2">Detail</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-muted-foreground">
                  <Loader2 className="mx-auto h-4 w-4 animate-spin" />
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-muted-foreground">
                  No audit entries match.
                </td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.id} className="border-t border-border align-top">
                <td className="px-3 py-2 whitespace-nowrap text-xs text-muted-foreground">
                  {new Date(r.created_at).toLocaleString()}
                </td>
                <td className="px-3 py-2 font-mono text-xs">
                  {r.actor?.slice(0, 8) ?? "—"}
                </td>
                <td className="px-3 py-2 font-mono text-xs">{r.action}</td>
                <td className="px-3 py-2 text-xs">
                  <div>{r.target_type ?? "—"}</div>
                  <div className="font-mono text-muted-foreground">
                    {r.target_id ?? ""}
                  </div>
                </td>
                <td className="px-3 py-2 max-w-[420px]">
                  {(r.before || r.after || r.meta) && (
                    <details>
                      <summary className="cursor-pointer text-xs text-muted-foreground">
                        Inspect
                      </summary>
                      <pre className="mt-2 max-h-48 overflow-auto rounded bg-muted p-2 text-[11px]">
                        {JSON.stringify(
                          { before: r.before, after: r.after, meta: r.meta },
                          null,
                          2,
                        )}
                      </pre>
                    </details>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
