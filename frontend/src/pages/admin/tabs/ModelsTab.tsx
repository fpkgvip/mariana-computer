import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { adminApi, FeatureFlag } from "@/lib/adminApi";
import { SectionHeader } from "../AdminShell";

/**
 * Model administration.  Rather than maintaining a separate table, model
 * access is controlled through feature flags with a "model." prefix or the
 * existing `flagship_models_all_plans` style flags.  This tab surfaces the
 * known model-related flags in one place.
 */
const MODEL_FLAG_KEYS: { key: string; label: string; description: string }[] = [
  {
    key: "flagship_models_all_plans",
    label: "Flagship models for all plans",
    description:
      "When enabled, users on any plan can select flagship models (e.g. Opus, GPT-5.4). Default: false (restricted to Team/Enterprise).",
  },
  {
    key: "image_gen",
    label: "Image generation",
    description: "Enables image generation tool (Nano Banana via LLM gateway).",
  },
  {
    key: "video_gen",
    label: "Video generation",
    description: "Enables video generation tool (Veo via LLM gateway).",
  },
  {
    key: "sub_agents",
    label: "Sub-agents",
    description: "Allows the planner to spawn sub-agents for parallel work.",
  },
  {
    key: "deep_tier",
    label: "Deep tier",
    description: "Allows the deepest research tier (higher budget, more models).",
  },
  {
    key: "web_publish",
    label: "Web publish",
    description: "Allows users to publish outputs as public web pages.",
  },
];

export function ModelsTab() {
  const [flags, setFlags] = useState<FeatureFlag[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await adminApi.listFlags();
      setFlags(Array.isArray(data) ? data : []);
    } catch (err) {
      toast.error("Failed to load model flags", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function toggle(key: string, next: boolean, description?: string) {
    setBusyKey(key);
    try {
      await adminApi.upsertFlag({ key, enabled: next, description });
      toast.success(`${key} = ${next ? "on" : "off"}`);
      refresh();
    } catch (err) {
      toast.error("Failed to update flag", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <div>
      <SectionHeader
        title="Models & capabilities"
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

      <p className="mb-4 max-w-2xl text-sm text-muted-foreground">
        Model access is gated by feature flags. Toggling here instantly affects
        new requests across all users. For full flag CRUD (custom keys,
        JSON values, etc.) use the <strong>Feature Flags</strong> tab.
      </p>

      <div className="space-y-3">
        {MODEL_FLAG_KEYS.map((def) => {
          const flag = flags.find((f) => f.key === def.key);
          const enabled = flag?.enabled ?? false;
          return (
            <div
              key={def.key}
              className="flex items-start justify-between gap-4 rounded-lg border border-border bg-card p-4"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <h3 className="font-medium">{def.label}</h3>
                  <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
                    {def.key}
                  </code>
                </div>
                <p className="mt-1 text-sm text-muted-foreground">
                  {def.description}
                </p>
              </div>
              <label className="flex shrink-0 items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={enabled}
                  disabled={busyKey === def.key}
                  onChange={(e) => toggle(def.key, e.target.checked, def.description)}
                  className="h-5 w-5"
                />
                <span>{enabled ? "Enabled" : "Disabled"}</span>
              </label>
            </div>
          );
        })}
      </div>

      <details className="mt-6 rounded-lg border border-border bg-card p-4">
        <summary className="cursor-pointer text-sm font-medium">
          All feature flags ({flags.length})
        </summary>
        <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
          {flags.map((f) => (
            <div
              key={f.key}
              className="rounded border border-border p-2 font-mono"
            >
              <span
                className={`mr-2 inline-block h-2 w-2 rounded-full ${
                  f.enabled ? "bg-emerald-500" : "bg-muted-foreground/40"
                }`}
              />
              {f.key}
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}

