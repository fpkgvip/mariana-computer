/**
 * F3 — Live Canvas
 *
 * Three split panes for an executing agent task:
 *  - Plan: enumerated steps with status
 *  - Activity: streaming event log (browser/sandbox/terminal)
 *  - Artifacts: files + links produced
 *
 * Receives events from a parent-managed EventSource (via the `events` prop)
 * so the page can decide when to stream vs poll. Resilient to reload — events
 * are also fetched once via REST on mount so a refresh doesn't show empty.
 *
 * Cancel button always live; ETA + credits-burned counter top-right.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Circle,
  Clock,
  Download,
  FileText,
  Loader2,
  PauseCircle,
  Play,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { AgentEvent, AgentTaskState } from "@/lib/agentRunApi";

interface LiveCanvasProps {
  task: AgentTaskState;
  events: AgentEvent[];
  /** Connection status: 'live' (SSE open), 'polling' (REST fallback), 'closed'. */
  connectionStatus?: "live" | "polling" | "closed";
  /** Reserved — cancel is owned by the StudioHeader strip; this prop is kept for backwards-compat. */
  onCancel?: () => void;
  /** Format a server timestamp string for display. */
  formatTime?: (iso: string) => string;
  className?: string;
}

interface PlanStep {
  id: string | number;
  description: string;
  status: "pending" | "running" | "done" | "error";
}

function stepsFromTaskOrEvents(
  task: AgentTaskState,
  events: AgentEvent[],
): PlanStep[] {
  // Prefer the task's `steps` snapshot when present.
  const taskSteps = (task.steps ?? []) as Array<Record<string, unknown>>;
  if (Array.isArray(taskSteps) && taskSteps.length > 0) {
    return taskSteps.map((s, i) => ({
      id: (s.id as string) ?? i,
      description:
        (s.description as string) ??
        (s.goal as string) ??
        (s.title as string) ??
        `Step ${i + 1}`,
      status:
        (s.status as PlanStep["status"]) ??
        (s.state as PlanStep["status"]) ??
        "pending",
    }));
  }
  // Fallback: derive from `step_started`/`step_finished` events.
  const stepMap = new Map<string, PlanStep>();
  for (const ev of events) {
    const p = ev.payload ?? {};
    const sid = (p.step_id as string) ?? (p.id as string);
    const desc = (p.description as string) ?? (p.goal as string);
    if (!sid) continue;
    const cur = stepMap.get(sid) ?? { id: sid, description: desc ?? sid, status: "pending" as const };
    if (ev.event_type === "step_started") cur.status = "running";
    if (ev.event_type === "step_finished") cur.status = "done";
    if (ev.event_type === "step_failed") cur.status = "error";
    if (desc) cur.description = desc;
    stepMap.set(sid, cur);
  }
  return Array.from(stepMap.values());
}

function isTerminalState(state: string): boolean {
  return ["done", "completed", "failed", "stopped", "cancelled", "error"].includes(state);
}

export function LiveCanvas({
  task,
  events,
  connectionStatus = "live",
  onCancel,
  formatTime,
  className,
}: LiveCanvasProps) {
  void onCancel; // Cancel button now lives in StudioHeader; prop kept for back-compat.
  const [tab, setTab] = useState<"plan" | "activity" | "artifacts">("plan");
  const activityRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const [collapsed, setCollapsed] = useState(false);

  // Auto-scroll activity to bottom on new events when enabled
  useEffect(() => {
    if (!autoScroll || tab !== "activity") return;
    const el = activityRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [events, autoScroll, tab]);

  const plan = useMemo(() => stepsFromTaskOrEvents(task, events), [task, events]);
  const artifacts = (task.artifacts ?? []) as Array<Record<string, unknown>>;

  const spentCredits = Math.max(0, Math.round((task.spent_usd ?? 0) * 100));
  const budgetCredits = Math.max(0, Math.round((task.budget_usd ?? 0) * 100));
  const burnPct = budgetCredits > 0 ? Math.min(100, (spentCredits / budgetCredits) * 100) : 0;

  const terminal = isTerminalState(task.state);
  const stageStatus: "plan" | "write" | "compile" | "verify" | "live" =
    terminal && task.state !== "failed" ? "live" : deriveRailStage(events, terminal, task.state);

  return (
    <div
      className={cn(
        "flex h-full min-h-[480px] flex-col rounded-xl border border-border bg-card shadow-sm",
        className,
      )}
      role="region"
      aria-label="Live canvas"
    >
      {/* Five-stage rail */}
      <StageRail current={stageStatus} terminal={terminal} failed={task.state === "failed"} />

      {/* Burn bar */}
      {budgetCredits > 0 && (
        <div
          className="h-0.5 w-full bg-secondary"
          role="progressbar"
          aria-label="Credit burn"
          aria-valuenow={spentCredits}
          aria-valuemin={0}
          aria-valuemax={budgetCredits}
        >
          <div
            className={cn(
              "h-full transition-all duration-300",
              burnPct < 80 ? "bg-accent" : burnPct < 100 ? "bg-warning" : "bg-destructive",
            )}
            style={{ width: `${burnPct}%` }}
          />
        </div>
      )}
      {/* Connection hint when polling/closed (calm, not alarming) */}
      {!terminal && connectionStatus !== "live" && (
        <div className="flex items-center gap-1.5 border-b border-border/60 bg-secondary/30 px-3 py-1 text-[11px] text-muted-foreground">
          <span className="size-1.5 rounded-full bg-warning" aria-hidden />
          {connectionStatus === "polling" ? "Polling — stream is reconnecting." : "Disconnected from stream."}
        </div>
      )}

      {/* Tabs */}
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <div role="tablist" aria-label="Canvas panes" className="flex gap-1">
          {(["plan", "activity", "artifacts"] as const).map((t) => (
            <button
              key={t}
              role="tab"
              type="button"
              aria-selected={tab === t}
              onClick={() => {
                setTab(t);
                if (collapsed) setCollapsed(false);
              }}
              className={cn(
                "rounded-md px-3 py-1 text-xs font-medium capitalize transition-colors",
                "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                tab === t
                  ? "bg-secondary text-foreground"
                  : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground",
              )}
            >
              {t}
              {t === "plan" && plan.length > 0 && <span className="ml-1">({plan.length})</span>}
              {t === "activity" && events.length > 0 && <span className="ml-1">({events.length})</span>}
              {t === "artifacts" && artifacts.length > 0 && <span className="ml-1">({artifacts.length})</span>}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? "Expand panel" : "Collapse panel"}
          aria-expanded={!collapsed}
          className={cn(
            "inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors",
            "hover:bg-secondary hover:text-foreground",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
          )}
        >
          {collapsed ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronUp size={14} aria-hidden="true" />}
        </button>
      </div>

      {/* Pane content */}
      <div
        className={cn(
          "relative flex-1 overflow-hidden transition-[max-height] duration-200",
          collapsed && "hidden",
        )}
        aria-hidden={collapsed}
      >
        {tab === "plan" && (
          <div className="h-full overflow-auto p-3">
            {plan.length === 0 ? (
              <EmptyState
                icon={<Loader2 size={16} className="animate-spin" />}
                title="Planning…"
                hint="Deft is breaking your prompt into steps."
              />
            ) : (
              <ol className="space-y-1.5">
                {plan.map((s, i) => (
                  <li
                    key={s.id}
                    className="flex items-start gap-2 rounded-md border border-transparent px-2 py-2 text-sm hover:border-border hover:bg-secondary/40"
                  >
                    <StepIcon status={s.status} />
                    <div className="min-w-0 flex-1">
                      <div className="text-foreground">
                        <span className="mr-2 inline-block w-5 text-right font-mono text-[10px] text-muted-foreground">
                          {String(i + 1).padStart(2, "0")}
                        </span>
                        {s.description}
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </div>
        )}

        {tab === "activity" && (
          <div className="flex h-full flex-col">
            <div className="flex items-center justify-end gap-3 px-3 py-1 text-[11px] text-muted-foreground">
              <label className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={autoScroll}
                  onChange={(e) => setAutoScroll(e.target.checked)}
                  className="h-3 w-3 accent-[hsl(var(--accent))]"
                />
                Auto-scroll
              </label>
            </div>
            <div ref={activityRef} className="flex-1 overflow-auto px-3 pb-3 font-mono text-xs leading-5">
              {events.length === 0 ? (
                <EmptyState
                  icon={<Loader2 size={16} className="animate-spin" />}
                  title="Waiting for first event…"
                  hint="Streaming will start within a couple seconds."
                />
              ) : (
                <ul className="space-y-0.5">
                  {events.map((e) => (
                    <li key={e.id} className="flex items-start gap-2">
                      <span className="shrink-0 text-[hsl(var(--fg-3))]">
                        {formatTime ? formatTime(e.created_at) : new Date(e.created_at).toLocaleTimeString()}
                      </span>
                      <span
                        className={cn(
                          "shrink-0 rounded px-1 py-px text-[10px] uppercase tracking-wide",
                          eventChipClass(e.event_type),
                        )}
                      >
                        {e.event_type}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-foreground">
                        {summarizeEventPayload(e)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}

        {tab === "artifacts" && (
          <div className="h-full overflow-auto p-3">
            {artifacts.length === 0 ? (
              <EmptyState
                icon={<FileText size={16} />}
                title="No artifacts yet"
                hint="Files and links will appear as Deft writes them."
              />
            ) : (
              <ul className="grid gap-2 md:grid-cols-2">
                {artifacts.map((a, i) => {
                  const path = (a.path as string) ?? (a.name as string) ?? `artifact-${i}`;
                  const url = (a.url as string) ?? (a.signed_url as string);
                  const size = a.size_bytes as number | undefined;
                  return (
                    <li key={path} className="rounded-lg border border-border bg-secondary/40 p-3">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="truncate text-sm font-medium text-foreground" title={path}>
                            {path}
                          </div>
                          {size !== undefined && (
                            <div className="text-[11px] text-muted-foreground">
                              {formatBytes(size)}
                            </div>
                          )}
                        </div>
                        {url && (
                          <a
                            href={url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-foreground hover:bg-secondary"
                          >
                            <Download size={12} aria-hidden />
                            Open
                          </a>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function StepIcon({ status }: { status: PlanStep["status"] }) {
  if (status === "done") return <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-success" aria-hidden />;
  if (status === "running") return <Loader2 size={14} className="mt-0.5 shrink-0 animate-spin text-accent" aria-hidden />;
  if (status === "error") return <AlertTriangle size={14} className="mt-0.5 shrink-0 text-destructive" aria-hidden />;
  return <Circle size={14} className="mt-0.5 shrink-0 text-muted-foreground" aria-hidden />;
}

function EmptyState({
  icon,
  title,
  hint,
}: {
  icon: React.ReactNode;
  title: string;
  hint: string;
}) {
  return (
    <div className="flex h-full min-h-[140px] flex-col items-center justify-center gap-1 text-center">
      <div className="text-muted-foreground">{icon}</div>
      <div className="text-sm font-medium text-foreground">{title}</div>
      <div className="text-xs text-muted-foreground">{hint}</div>
    </div>
  );
}

function eventChipClass(type: string): string {
  if (type.includes("error") || type.includes("failed")) return "bg-destructive/15 text-destructive";
  if (type.includes("approval")) return "bg-warning/15 text-warning";
  if (type.includes("tool") || type.includes("browser") || type.includes("sandbox"))
    return "bg-[hsl(var(--accent-muted))] text-accent-strong";
  if (type.includes("step_finished") || type.includes("done")) return "bg-success/15 text-success";
  return "bg-secondary text-muted-foreground";
}

function summarizeEventPayload(e: AgentEvent): string {
  const p = e.payload ?? {};
  if (typeof p.message === "string") return p.message;
  if (typeof p.summary === "string") return p.summary;
  if (typeof p.description === "string") return p.description;
  if (typeof p.tool === "string") {
    const args = p.args ? ` ${truncate(JSON.stringify(p.args), 160)}` : "";
    return `${p.tool}${args}`;
  }
  if (typeof p.error === "string") return `error: ${p.error}`;
  if (typeof p.path === "string") return p.path;
  // Fallback: compact JSON
  try {
    return truncate(JSON.stringify(p), 240);
  } catch {
    return "(opaque payload)";
  }
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

// ---------------------------------------------------------------------------
// Five-stage rail — Plan → Write → Compile → Verify → Live
// ---------------------------------------------------------------------------

const RAIL_STAGES: ReadonlyArray<{ key: "plan" | "write" | "compile" | "verify" | "live"; label: string }> = [
  { key: "plan", label: "Plan" },
  { key: "write", label: "Write" },
  { key: "compile", label: "Compile" },
  { key: "verify", label: "Verify" },
  { key: "live", label: "Live" },
];

function StageRail({
  current,
  terminal,
  failed,
}: {
  current: "plan" | "write" | "compile" | "verify" | "live";
  terminal: boolean;
  failed: boolean;
}) {
  const currentIdx = RAIL_STAGES.findIndex((s) => s.key === current);
  return (
    <ol
      role="list"
      aria-label="Run progress"
      className="flex w-full items-center gap-1 border-b border-border/70 bg-surface-1/40 px-3 py-2"
    >
      {RAIL_STAGES.map((stage, i) => {
        const done = i < currentIdx || (terminal && !failed);
        const active = i === currentIdx && !terminal;
        const isLast = i === RAIL_STAGES.length - 1;
        return (
          <li key={stage.key} className="flex flex-1 items-center gap-1.5">
            <span
              aria-current={active ? "step" : undefined}
              className={cn(
                "flex items-center gap-1.5 text-[11px] font-medium tracking-wide",
                active && !failed && "text-accent-strong",
                active && failed && "text-destructive",
                done && "text-foreground",
                !done && !active && "text-muted-foreground",
              )}
            >
              <span
                className={cn(
                  "size-1.5 shrink-0 rounded-full",
                  done && "bg-deploy",
                  active && !failed && "bg-accent studio-breathe",
                  active && failed && "bg-destructive",
                  !done && !active && "bg-muted-foreground/40",
                )}
              />
              <span className="hidden sm:inline">{stage.label}</span>
            </span>
            {!isLast && (
              <span
                aria-hidden
                className={cn(
                  "h-px flex-1",
                  i < currentIdx ? "bg-deploy/50" : "bg-border",
                )}
              />
            )}
          </li>
        );
      })}
    </ol>
  );
}

function deriveRailStage(
  events: AgentEvent[],
  terminal: boolean,
  state: string,
): "plan" | "write" | "compile" | "verify" | "live" {
  if (terminal && state !== "failed") return "live";
  // Walk recent events backward to find the strongest signal.
  const window = events.slice(-80);
  for (let i = window.length - 1; i >= 0; i--) {
    const e = window[i];
    const tool = (e.payload?.tool ?? e.payload?.tool_name ?? "") as string;
    const evType = e.event_type;
    if (tool === "deploy_preview" || evType === "deploy_started") return "verify";
    if (tool === "browser_screenshot" || evType === "verify" || tool === "browser_open") return "verify";
    if (
      tool === "sandbox_exec" ||
      tool === "exec" ||
      /\b(?:vite|tsc|npm|pnpm|build|compile)\b/.test(JSON.stringify(e.payload ?? {}))
    )
      return "compile";
    if (tool === "fs_write" || tool === "fs_edit" || evType === "step_started") return "write";
    if (evType === "plan" || tool === "plan") return "plan";
  }
  return "plan";
}

// Re-export useful types/icons so the parent doesn't need to import from lucide directly
export { Play, PauseCircle, Clock };
