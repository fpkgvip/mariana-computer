import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { useAuth } from "@/contexts/AuthContext";
import { supabase } from "@/lib/supabase";
import { Loader2, Inbox, CheckCircle2, AlertCircle, Clock, Play, ArrowRight, RefreshCw } from "lucide-react";
import {
  EmptyState,
  ErrorState,
  LoadingRows,
  describeError,
} from "@/components/deft/states";
import { ApiError } from "@/lib/api";

const API_URL = import.meta.env.VITE_API_URL ?? "";

interface AgentTaskSummary {
  id: string;
  goal: string;
  state: string;
  selected_model: string;
  budget_usd: number;
  spent_usd: number;
  replan_count: number;
  total_failures: number;
  error: string | null;
  has_final_answer: boolean;
  created_at: string;
  updated_at: string;
}

interface TasksListResponse {
  total: number;
  limit: number;
  offset: number;
  tasks: AgentTaskSummary[];
}

type StateFilter = "all" | "running" | "done" | "failed";

const STATE_BUCKETS: Record<StateFilter, string[]> = {
  all: [],
  running: ["plan", "execute", "test", "deliver", "fix"],
  done: ["done"],
  failed: ["failed", "halted"],
};

const FILTER_LABELS: Record<StateFilter, string> = {
  all: "All",
  running: "Running",
  done: "Completed",
  failed: "Failed / halted",
};

function stateStyle(state: string) {
  const s = state.toLowerCase();
  if (s === "done") {
    return {
      icon: <CheckCircle2 size={13} />,
      label: "Done",
      className: "bg-emerald-500/10 text-emerald-400 ring-emerald-500/20",
    };
  }
  if (s === "failed" || s === "halted") {
    return {
      icon: <AlertCircle size={13} />,
      label: s === "halted" ? "Halted" : "Failed",
      className: "bg-red-500/10 text-red-400 ring-red-500/20",
    };
  }
  if (s === "plan" || s === "queued") {
    return {
      icon: <Clock size={13} />,
      label: "Queued",
      className: "bg-zinc-500/10 text-zinc-400 ring-zinc-500/20",
    };
  }
  return {
    icon: <Play size={13} />,
    label: s.charAt(0).toUpperCase() + s.slice(1),
    className: "bg-blue-500/10 text-blue-400 ring-blue-500/20",
  };
}

function formatRelative(iso: string): string {
  try {
    const then = new Date(iso).getTime();
    const now = Date.now();
    const diff = now - then;
    const m = Math.round(diff / 60000);
    if (m < 1) return "just now";
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.round(h / 24);
    if (d < 30) return `${d}d ago`;
    return new Date(iso).toLocaleDateString();
  } catch {
    return "";
  }
}

export default function Tasks() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [filter, setFilter] = useState<StateFilter>("all");
  const [tasks, setTasks] = useState<AgentTaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [total, setTotal] = useState(0);
  const abortRef = useRef<AbortController | null>(null);

  // Grace period for auth refresh, mirroring Account.tsx.
  useEffect(() => {
    if (!user) {
      const t = setTimeout(() => navigate("/login", { replace: true }), 500);
      return () => clearTimeout(t);
    }
  }, [user, navigate]);

  const fetchTasks = async (silent = false) => {
    if (!silent) setLoading(true);
    setRefreshing(silent);
    setError(null);
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const { data: { session } } = await supabase.auth.getSession();
      const token = session?.access_token;
      if (!token) {
        navigate("/login");
        return;
      }
      const res = await fetch(`${API_URL}/api/agent?limit=100`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: ac.signal,
      });
      if (!res.ok) {
        const requestId = res.headers.get("x-request-id");
        const text = await res.text().catch(() => res.statusText);
        throw new ApiError(res.status, `HTTP ${res.status}: ${text}`, null, requestId);
      }
      const data: TasksListResponse = await res.json();
      setTasks(Array.isArray(data.tasks) ? data.tasks : []);
      setTotal(Number(data.total) || 0);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(describeError(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
      setRetrying(false);
    }
  };

  useEffect(() => {
    if (!user) return;
    fetchTasks(false);
    // Poll every 10s while on page; cheap and keeps running tasks current.
    const iv = setInterval(() => fetchTasks(true), 10_000);
    return () => {
      clearInterval(iv);
      abortRef.current?.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  const filtered = useMemo(() => {
    if (filter === "all") return tasks;
    const wanted = new Set(STATE_BUCKETS[filter]);
    return tasks.filter((t) => wanted.has(t.state.toLowerCase()));
  }, [tasks, filter]);

  const counts = useMemo(() => {
    const c: Record<StateFilter, number> = { all: tasks.length, running: 0, done: 0, failed: 0 };
    for (const t of tasks) {
      const s = t.state.toLowerCase();
      if (STATE_BUCKETS.running.includes(s)) c.running++;
      else if (STATE_BUCKETS.done.includes(s)) c.done++;
      else if (STATE_BUCKETS.failed.includes(s)) c.failed++;
    }
    return c;
  }, [tasks]);

  if (!user) {
    return (
      <div className="min-h-screen bg-background">
        <Navbar />
        <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
          <div className="mx-auto max-w-4xl">
            <div className="h-8 w-40 animate-pulse rounded bg-muted" />
            <div className="mt-8 h-64 animate-pulse rounded-lg border border-border bg-card/50" />
          </div>
        </section>
        <Footer />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
        <div className="mx-auto max-w-4xl">
          <ScrollReveal>
            <div className="flex items-center justify-between gap-4">
              <div>
                <h1 className="font-serif text-2xl font-semibold text-foreground sm:text-3xl">
                  Tasks
                </h1>
                <p className="mt-2 text-sm text-muted-foreground">
                  Every agent task you've kicked off. Streams stay live, and terminated runs are kept for audit.
                </p>
              </div>
              <button
                onClick={() => fetchTasks(false)}
                disabled={loading || refreshing}
                className="inline-flex items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground disabled:opacity-60"
                aria-label="Refresh"
              >
                {refreshing ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : (
                  <RefreshCw size={13} />
                )}
                Refresh
              </button>
            </div>
          </ScrollReveal>

          {/* Filter tabs */}
          <ScrollReveal>
            <div className="mt-8 flex flex-wrap gap-2">
              {(Object.keys(FILTER_LABELS) as StateFilter[]).map((key) => (
                <button
                  key={key}
                  onClick={() => setFilter(key)}
                  className={`inline-flex items-center gap-2 rounded-full px-4 py-1.5 text-xs font-medium ring-1 transition-colors ${
                    filter === key
                      ? "bg-primary/10 text-primary ring-primary/30"
                      : "bg-card text-muted-foreground ring-border hover:text-foreground"
                  }`}
                >
                  {FILTER_LABELS[key]}
                  <span className="tabular-nums opacity-70">{counts[key]}</span>
                </button>
              ))}
            </div>
          </ScrollReveal>

          {/* Content */}
          <div className="mt-8">
            {loading ? (
              <LoadingRows count={3} rowClassName="h-20" />
            ) : error ? (
              <ErrorState
                title="Could not load tasks"
                message={error.message}
                requestId={error.requestId}
                onRetry={() => {
                  setRetrying(true);
                  void fetchTasks(false);
                }}
                retrying={retrying}
              />
            ) : filtered.length === 0 ? (
              <ScrollReveal>
                <EmptyState
                  icon={<Inbox size={20} aria-hidden />}
                  filtered={filter !== "all"}
                  title={
                    filter === "all"
                      ? "No tasks yet"
                      : `No ${FILTER_LABELS[filter].toLowerCase()} tasks`
                  }
                  description={
                    filter === "all"
                      ? "Kick one off from Research to get started."
                      : "Try a different filter."
                  }
                  action={
                    filter === "all" ? (
                      <Link
                        to="/chat"
                        className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-all hover:bg-primary/90"
                      >
                        Start a task <ArrowRight size={14} aria-hidden />
                      </Link>
                    ) : undefined
                  }
                />
              </ScrollReveal>
            ) : (
              <ul className="space-y-2">
                {filtered.map((t) => {
                  const s = stateStyle(t.state);
                  return (
                    <li key={t.id}>
                      <Link
                        to={`/tasks/${t.id}`}
                        className="group flex items-start gap-4 rounded-lg border border-border bg-card p-4 transition-colors hover:border-primary/40 hover:bg-secondary/30"
                      >
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider ring-1 ring-inset ${s.className}`}>
                              {s.icon}
                              {s.label}
                            </span>
                            <span className="text-[11px] text-muted-foreground">
                              {formatRelative(t.created_at)}
                            </span>
                          </div>
                          <p className="mt-2 truncate text-sm font-medium text-foreground">
                            {t.goal}
                          </p>
                          <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                            {t.selected_model} · ${t.spent_usd.toFixed(3)} of ${t.budget_usd.toFixed(2)}
                            {t.replan_count > 0 && ` · ${t.replan_count} replan${t.replan_count === 1 ? "" : "s"}`}
                            {t.total_failures > 0 && ` · ${t.total_failures} failure${t.total_failures === 1 ? "" : "s"}`}
                          </p>
                          {t.error && (
                            <p className="mt-1 truncate text-[11px] text-red-400">
                              {t.error}
                            </p>
                          )}
                        </div>
                        <ArrowRight
                          size={15}
                          className="mt-1 shrink-0 text-muted-foreground transition-all group-hover:translate-x-0.5 group-hover:text-foreground"
                        />
                      </Link>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          {total > tasks.length && (
            <p className="mt-6 text-center text-xs text-muted-foreground">
              Showing {tasks.length} of {total}. Older tasks are retained and paginated.
            </p>
          )}
        </div>
      </section>

      <Footer />
    </div>
  );
}
