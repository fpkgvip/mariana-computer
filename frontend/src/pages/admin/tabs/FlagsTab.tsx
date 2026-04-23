import { useCallback, useEffect, useState } from "react";
import { Loader2, Plus, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { adminApi, FeatureFlag } from "@/lib/adminApi";
import { SectionHeader } from "../AdminShell";

export function FlagsTab() {
  const [flags, setFlags] = useState<FeatureFlag[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  // New-flag form state
  const [newKey, setNewKey] = useState("");
  const [newEnabled, setNewEnabled] = useState(true);
  const [newDesc, setNewDesc] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await adminApi.listFlags();
      setFlags(Array.isArray(data) ? data : []);
    } catch (err) {
      toast.error("Failed to load flags", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function toggle(f: FeatureFlag) {
    setBusyKey(f.key);
    try {
      await adminApi.upsertFlag({
        key: f.key,
        enabled: !f.enabled,
        description: f.description ?? undefined,
      });
      toast.success(`${f.key} = ${!f.enabled ? "on" : "off"}`);
      refresh();
    } catch (err) {
      toast.error("Failed to toggle", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyKey(null);
    }
  }

  async function remove(key: string) {
    if (!confirm(`Delete flag "${key}"?`)) return;
    setBusyKey(key);
    try {
      await adminApi.deleteFlag(key);
      toast.success("Deleted");
      refresh();
    } catch (err) {
      toast.error("Failed to delete", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyKey(null);
    }
  }

  async function create(e: React.FormEvent) {
    e.preventDefault();
    const key = newKey.trim();
    if (!key) {
      toast.error("Key required");
      return;
    }
    if (!/^[a-zA-Z0-9_.-]{1,128}$/.test(key)) {
      toast.error("Invalid key: use [a-zA-Z0-9_.-] only");
      return;
    }
    setBusyKey(key);
    try {
      await adminApi.upsertFlag({
        key,
        enabled: newEnabled,
        description: newDesc.trim() || undefined,
      });
      toast.success(`Created/updated ${key}`);
      setNewKey("");
      setNewDesc("");
      setNewEnabled(true);
      refresh();
    } catch (err) {
      toast.error("Failed to save", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <div>
      <SectionHeader
        title={`Feature flags (${flags.length})`}
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

      <form
        onSubmit={create}
        className="mb-6 flex flex-wrap items-end gap-3 rounded-lg border border-dashed border-border bg-card/50 p-4"
      >
        <div className="min-w-[220px] flex-1">
          <label className="mb-1 block text-xs font-medium">Key</label>
          <input
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
            placeholder="e.g. my_feature_enabled"
            className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          />
        </div>
        <div className="min-w-[280px] flex-[2]">
          <label className="mb-1 block text-xs font-medium">Description</label>
          <input
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
            placeholder="What does this flag do?"
            className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          />
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={newEnabled}
            onChange={(e) => setNewEnabled(e.target.checked)}
          />
          Enabled
        </label>
        <button
          type="submit"
          className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
        >
          <Plus className="h-4 w-4" />
          Add / upsert
        </button>
      </form>

      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-muted/40 text-left">
            <tr>
              <th className="px-3 py-2">Key</th>
              <th className="px-3 py-2">Enabled</th>
              <th className="px-3 py-2">Description</th>
              <th className="px-3 py-2">Updated</th>
              <th className="px-3 py-2">Actions</th>
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
            {!loading && flags.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-muted-foreground">
                  No flags yet.
                </td>
              </tr>
            )}
            {flags.map((f) => (
              <tr key={f.key} className="border-t border-border">
                <td className="px-3 py-2 font-mono">{f.key}</td>
                <td className="px-3 py-2">
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={f.enabled}
                      disabled={busyKey === f.key}
                      onChange={() => toggle(f)}
                    />
                    <span
                      className={
                        f.enabled
                          ? "text-emerald-500"
                          : "text-muted-foreground"
                      }
                    >
                      {f.enabled ? "on" : "off"}
                    </span>
                  </label>
                </td>
                <td className="px-3 py-2 text-muted-foreground">
                  {f.description ?? "—"}
                </td>
                <td className="px-3 py-2 text-xs text-muted-foreground">
                  {f.updated_at
                    ? new Date(f.updated_at).toLocaleString()
                    : "—"}
                </td>
                <td className="px-3 py-2">
                  <button
                    onClick={() => remove(f.key)}
                    disabled={busyKey === f.key}
                    className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-destructive hover:bg-destructive/10 disabled:opacity-50"
                  >
                    <Trash2 className="h-3 w-3" />
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
