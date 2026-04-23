import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw, CheckCircle2, XCircle } from "lucide-react";
import { toast } from "sonner";
import { adminApi, HealthProbeResult } from "@/lib/adminApi";
import { SectionHeader } from "../AdminShell";

export function HealthTab() {
  const [data, setData] = useState<HealthProbeResult | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const d = await adminApi.healthProbe();
      setData(d);
    } catch (err) {
      toast.error("Health probe failed", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 60_000);
    return () => clearInterval(id);
  }, [refresh]);

  const entries = Object.entries(data?.components ?? {});

  return (
    <div>
      <SectionHeader
        title="System health"
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
            Probe again
          </button>
        }
      />

      {data && (
        <div className="mb-4 flex items-center gap-3 rounded-lg border border-border bg-card p-4">
          {data.ok ? (
            <CheckCircle2 className="h-6 w-6 text-emerald-500" />
          ) : (
            <XCircle className="h-6 w-6 text-destructive" />
          )}
          <div>
            <p className="font-medium">
              {data.ok ? "All systems nominal" : "Degraded"}
            </p>
            <p className="text-xs text-muted-foreground">
              Last checked {new Date(data.timestamp).toLocaleString()}
            </p>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {entries.map(([name, c]) => (
          <div
            key={name}
            className="flex items-start justify-between gap-3 rounded-lg border border-border bg-card p-4"
          >
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                {c.ok ? (
                  <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
                ) : (
                  <XCircle className="h-4 w-4 shrink-0 text-destructive" />
                )}
                <h3 className="font-medium capitalize">{name.replace("_", " ")}</h3>
              </div>
              <p className="mt-1 break-words text-xs text-muted-foreground">
                {c.detail}
              </p>
            </div>
            <div className="shrink-0 text-right font-mono text-xs text-muted-foreground">
              {c.latency_ms}ms
            </div>
          </div>
        ))}
      </div>

      {loading && !data && (
        <p className="mt-6 text-sm text-muted-foreground">
          <Loader2 className="mr-2 inline h-4 w-4 animate-spin" />
          Probing…
        </p>
      )}
    </div>
  );
}
