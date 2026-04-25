/**
 * F5 — Projects sidebar
 *
 * Lists the user's recent agent tasks (one task per row). The page treats
 * each task as a "project" for now — we'll add proper grouping in v1.1 once
 * the backend has a `projects` table.
 *
 * Behaviors:
 *  - "+ New run" CTA at top resets the page to an empty prompt
 *  - Shows current credit balance
 *  - Filterable by free-text search (client-side)
 *  - Clicking a row navigates to ?task=<id>
 *  - Auto-refreshes every 8s when present
 */
import { useEffect, useMemo, useState } from "react";
import { Coins, FolderOpen, Loader2, Plus, Search, AlertTriangle, CheckCircle2 } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

interface AgentTaskRow {
  id: string;
  goal: string;
  state: string;
  selected_model: string;
  budget_usd: number;
  spent_usd: number;
  created_at: string;
  updated_at: string;
  has_final_answer: boolean;
  error: string | null;
}

interface AgentTaskList {
  total: number;
  limit: number;
  offset: number;
  tasks: AgentTaskRow[];
}

interface ProjectsSidebarProps {
  activeTaskId: string | null;
  onSelect: (taskId: string) => void;
  onNew: () => void;
  balance: number;
}

const POLL_INTERVAL_MS = 8_000;

export function ProjectsSidebar({ activeTaskId, onSelect, onNew, balance }: ProjectsSidebarProps) {
  const [tasks, setTasks] = useState<AgentTaskRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const resp = await api.get<AgentTaskList>("/api/agent?limit=50");
        if (cancelled) return;
        setTasks(resp.tasks);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not load projects");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    const id = window.setInterval(load, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return tasks;
    return tasks.filter((t) => t.goal.toLowerCase().includes(q));
  }, [filter, tasks]);

  return (
    <aside
      aria-label="Projects"
      className="flex h-full w-60 shrink-0 flex-col border-r border-border bg-[hsl(var(--sidebar-background))]"
    >
      <div className="border-b border-border px-3 py-3">
        <button
          type="button"
          onClick={onNew}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-accent-foreground transition-opacity hover:opacity-90"
        >
          <Plus size={14} aria-hidden /> New run
        </button>
        <div className="mt-3 flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
          <Coins size={12} aria-hidden />
          <span>
            <span className="font-mono text-foreground">{balance.toLocaleString()}</span> credits
          </span>
        </div>
      </div>

      <div className="border-b border-border px-3 py-2">
        <label className="relative block">
          <Search
            size={12}
            aria-hidden
            className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground"
          />
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter projects"
            aria-label="Filter projects"
            className="w-full rounded-md border border-border bg-input py-1.5 pl-7 pr-2 text-xs text-foreground outline-none placeholder:text-[hsl(var(--fg-3))] focus:border-accent"
          />
        </label>
      </div>

      <div className="flex-1 overflow-auto px-2 py-2">
        {loading ? (
          <div className="flex items-center justify-center gap-2 px-3 py-6 text-xs text-muted-foreground">
            <Loader2 size={12} className="animate-spin" aria-hidden /> Loading…
          </div>
        ) : error ? (
          <div className="flex items-start gap-2 px-2 py-3 text-xs text-destructive">
            <AlertTriangle size={12} className="mt-0.5 shrink-0" aria-hidden />
            <span>{error}</span>
          </div>
        ) : visible.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-1 px-3 py-10 text-center text-xs text-muted-foreground">
            <FolderOpen size={20} aria-hidden />
            <div>{filter ? "No matches" : "No runs yet"}</div>
            <div className="opacity-70">Start your first run from the prompt bar.</div>
          </div>
        ) : (
          <ul className="space-y-0.5">
            {visible.map((t) => (
              <li key={t.id}>
                <button
                  type="button"
                  onClick={() => onSelect(t.id)}
                  aria-current={activeTaskId === t.id ? "true" : undefined}
                  className={cn(
                    "flex w-full items-start gap-2 rounded-md px-2 py-2 text-left text-xs transition-colors",
                    activeTaskId === t.id
                      ? "bg-secondary text-foreground"
                      : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground",
                  )}
                >
                  <StateDot state={t.state} />
                  <span className="min-w-0 flex-1">
                    <span className="line-clamp-2 text-[12px] leading-tight text-foreground">
                      {t.goal}
                    </span>
                    <span className="mt-1 flex items-center gap-1.5 text-[10px] text-muted-foreground">
                      <span>{relativeTime(t.created_at)}</span>
                      <span className="opacity-50">·</span>
                      <span>${t.spent_usd.toFixed(2)}</span>
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}

function StateDot({ state }: { state: string }) {
  const cls = (() => {
    switch (state) {
      case "done":
      case "completed":
        return "bg-success";
      case "failed":
      case "error":
        return "bg-destructive";
      case "stopped":
      case "cancelled":
        return "bg-muted-foreground";
      case "plan":
      case "act":
      case "running":
        return "bg-accent animate-pulse";
      default:
        return "bg-muted-foreground";
    }
  })();
  return <span className={cn("mt-1 h-2 w-2 shrink-0 rounded-full", cls)} aria-label={state} />;
}

function relativeTime(iso: string): string {
  try {
    const ts = new Date(iso).getTime();
    if (!Number.isFinite(ts)) return "";
    const diff = Math.floor((Date.now() - ts) / 1000);
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86_400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86_400)}d ago`;
  } catch {
    return "";
  }
}
