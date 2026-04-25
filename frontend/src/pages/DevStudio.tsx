/**
 * Dev-only studio preview route.
 *
 * Renders the studio chrome with mock data, no auth required, for visual
 * iteration at multiple viewport widths.  Gated on import.meta.env.DEV in
 * App.tsx — this route is not registered in production builds.
 *
 * Use ?mode=idle | live | empty to switch states.
 */
import { useState, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Coins, Plus, Search } from "lucide-react";
import { cn } from "@/lib/utils";
import { StudioFrame } from "@/components/deft/studio/StudioFrame";
import { StudioHeader } from "@/components/deft/studio/StudioHeader";
import { IdleStudio } from "@/components/deft/studio/IdleStudio";
import { LiveStudio } from "@/components/deft/studio/LiveStudio";
import type { AgentEvent, AgentTaskState } from "@/lib/agentRunApi";
import type { ModelTier, QuoteResponse } from "@/lib/agentApi";

type Mode = "idle" | "live" | "empty" | "done";

const MOCK_TASK: AgentTaskState = {
  id: "tsk_dev_1",
  user_id: "u_dev",
  goal: "A habit tracker with a streak heatmap and Supabase auth",
  state: "running",
  selected_model: "claude-sonnet-4-6",
  budget_usd: 5.0,
  spent_usd: 1.42,
  steps: [
    { id: "s1", description: "Scaffold Vite + Tailwind + shadcn", status: "done" },
    { id: "s2", description: "Build streak heatmap component", status: "done" },
    { id: "s3", description: "Wire Supabase auth (email + magic link)", status: "running" },
    { id: "s4", description: "Add habit CRUD with optimistic updates", status: "pending" },
    { id: "s5", description: "Verify in browser, deploy preview", status: "pending" },
  ],
  artifacts: [],
  error: null,
  created_at: new Date(Date.now() - 4 * 60 * 1000).toISOString(),
  updated_at: new Date().toISOString(),
};

const MOCK_EVENTS: AgentEvent[] = [
  {
    id: 1,
    task_id: "tsk_dev_1",
    event_type: "step_started",
    payload: { step_id: "s1", description: "Scaffold Vite + Tailwind + shadcn" },
    created_at: new Date(Date.now() - 230_000).toISOString(),
  },
  {
    id: 2,
    task_id: "tsk_dev_1",
    event_type: "tool_started",
    payload: { tool: "fs_write", path: "package.json" },
    created_at: new Date(Date.now() - 220_000).toISOString(),
  },
  {
    id: 3,
    task_id: "tsk_dev_1",
    event_type: "step_finished",
    payload: { step_id: "s1" },
    created_at: new Date(Date.now() - 180_000).toISOString(),
  },
  {
    id: 4,
    task_id: "tsk_dev_1",
    event_type: "step_started",
    payload: { step_id: "s3", description: "Wire Supabase auth" },
    created_at: new Date(Date.now() - 60_000).toISOString(),
  },
  {
    id: 5,
    task_id: "tsk_dev_1",
    event_type: "tool_started",
    payload: { tool: "fs_edit", path: "src/lib/supabase.ts" },
    created_at: new Date(Date.now() - 30_000).toISOString(),
  },
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
] as any[];

export default function DevStudio() {
  if (!import.meta.env.DEV) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-foreground">
        <p className="text-sm text-muted-foreground">Dev preview disabled in production.</p>
      </div>
    );
  }

  const [params, setParams] = useSearchParams();
  const mode = (params.get("mode") as Mode | null) ?? "idle";
  const [draftPrompt, setDraftPrompt] = useState("");

  const goto = (m: Mode) => {
    const next = new URLSearchParams(params);
    next.set("mode", m);
    setParams(next, { replace: true });
  };

  const noopStart = async (_p: { tier: ModelTier; ceiling: number; quote: QuoteResponse }) => {
    void _p;
  };

  const headerProps = useMemo(() => {
    if (mode === "live") {
      return {
        title: MOCK_TASK.goal,
        stage: "compile" as const,
        spentCredits: 142,
        ceilingCredits: 500,
        durationSec: 247,
        canCancel: true,
        onCancel: () => alert("(dev) cancel requested"),
        onNewRun: () => goto("idle"),
      };
    }
    if (mode === "done") {
      return {
        title: MOCK_TASK.goal,
        stage: "live" as const,
        spentCredits: 312,
        ceilingCredits: 500,
        durationSec: 260,
        canCancel: false,
        onNewRun: () => goto("idle"),
      };
    }
    return {
      title: "",
      stage: "idle" as const,
    };
  }, [mode]);

  const doneTask: AgentTaskState = useMemo(
    () => ({
      ...MOCK_TASK,
      state: "done",
      spent_usd: 3.12,
      steps: MOCK_TASK.steps.map((s) => ({ ...s, status: "done" })),
      artifacts: [
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        { url: "https://preview.deft.computer/tsk_dev_1", path: "index.html" } as any,
      ],
    }),
    [],
  );

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <Navbar />
      <div className="mt-16 border-b border-border/60 bg-surface-1/40 px-3 py-2">
        <div className="mx-auto flex max-w-[1440px] items-center gap-2 text-[11px] text-muted-foreground">
          <span className="font-mono uppercase tracking-[0.16em]">/dev/studio</span>
          <span aria-hidden>·</span>
          <ModeButton current={mode} value="idle" onClick={() => goto("idle")} />
          <ModeButton current={mode} value="empty" onClick={() => goto("empty")} />
          <ModeButton current={mode} value="live" onClick={() => goto("live")} />
          <ModeButton current={mode} value="done" onClick={() => goto("done")} />
        </div>
      </div>

      <div className="flex flex-1 min-h-0 overflow-hidden">
        <StudioFrame
          projects={<MockProjectsSidebar onNewRun={() => goto("idle")} activeMode={mode} />}
          header={<StudioHeader {...headerProps} />}
        >
          {mode === "live" ? (
            <LiveStudio
              task={MOCK_TASK}
              events={MOCK_EVENTS}
              connectionStatus="live"
              onCancel={() => alert("(dev) cancel")}
              taskId={MOCK_TASK.id}
            />
          ) : mode === "done" ? (
            <LiveStudio
              task={doneTask}
              events={MOCK_EVENTS}
              connectionStatus="live"
              onCancel={() => {}}
              taskId={doneTask.id}
              previewUrl="https://preview.deft.computer/tsk_dev_1"
            />
          ) : (
            <IdleStudio
              prompt={mode === "empty" ? "" : draftPrompt}
              onPromptChange={setDraftPrompt}
              onStart={noopStart}
              balance={4_280}
              starting={false}
              unlimited={false}
            />
          )}
        </StudioFrame>
      </div>
    </div>
  );
}

function MockProjectsSidebar({
  onNewRun,
  activeMode,
}: {
  onNewRun: () => void;
  activeMode: Mode;
}) {
  const tasks = [
    { id: "tsk_dev_1", goal: "Habit tracker with streak heatmap and Supabase auth", state: "running", relative: "4m ago", spent: 1.42 },
    { id: "tsk_dev_2", goal: "Markdown editor with split-pane preview", state: "done", relative: "2h ago", spent: 3.10 },
    { id: "tsk_dev_3", goal: "Telegram bot summarizing unread emails", state: "failed", relative: "yesterday", spent: 0.84 },
    { id: "tsk_dev_4", goal: "Landing page for a payroll API", state: "archived", relative: "3d ago", spent: 2.20 },
  ];
  return (
    <aside
      aria-label="Projects"
      className="flex h-full w-60 shrink-0 flex-col border-r border-border bg-[hsl(var(--sidebar-background))]"
    >
      <div className="border-b border-border px-3 py-3">
        <button
          type="button"
          onClick={onNewRun}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-accent-foreground transition-opacity hover:opacity-90"
        >
          <Plus size={14} aria-hidden /> New run
        </button>
        <div className="mt-3 flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
          <Coins size={12} aria-hidden />
          <span>
            <span className="font-mono text-foreground">4,280</span> credits
          </span>
        </div>
      </div>
      <div className="border-b border-border px-3 py-2">
        <label className="relative block">
          <Search size={12} aria-hidden className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            placeholder="Filter projects"
            aria-label="Filter projects"
            className="w-full rounded-md border border-border bg-input py-1.5 pl-7 pr-2 text-xs text-foreground outline-none placeholder:text-[hsl(var(--fg-3))] focus:border-accent"
          />
        </label>
      </div>
      <ul className="flex-1 overflow-auto px-2 py-2 space-y-0.5">
        {tasks.map((t) => {
          const archived = t.state === "archived";
          const active = activeMode === "live" && t.id === "tsk_dev_1";
          return (
            <li key={t.id}>
              <button
                type="button"
                aria-current={active ? "true" : undefined}
                className={cn(
                  "flex w-full items-start gap-2 rounded-md px-2 py-2 text-left text-xs transition-colors",
                  active
                    ? "bg-secondary text-foreground"
                    : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground",
                )}
              >
                <span
                  className={cn(
                    "mt-1 h-2 w-2 shrink-0 rounded-full",
                    t.state === "done" && "bg-success",
                    t.state === "failed" && "bg-destructive",
                    t.state === "running" && "bg-accent studio-breathe",
                    t.state === "archived" && "bg-muted-foreground",
                  )}
                />
                <span className="min-w-0 flex-1">
                  <span className={cn("line-clamp-2 text-[12px] leading-tight", archived ? "text-muted-foreground italic" : "text-foreground")}>{t.goal}</span>
                  <span className="mt-1 flex items-center gap-1.5 text-[11px] text-muted-foreground">
                    <span>{t.relative}</span>
                    <span aria-hidden>·</span>
                    <span>${t.spent.toFixed(2)}</span>
                    {archived && (
                      <>
                        <span aria-hidden>·</span>
                        <span className="uppercase tracking-wide">archived</span>
                      </>
                    )}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </aside>
  );
}

function ModeButton({
  current,
  value,
  onClick,
}: {
  current: Mode;
  value: Mode;
  onClick: () => void;
}) {
  const active = current === value;
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "rounded px-2 py-0.5 font-mono uppercase tracking-[0.12em] transition-colors " +
        (active
          ? "bg-accent text-accent-foreground"
          : "text-muted-foreground hover:bg-secondary hover:text-foreground")
      }
    >
      {value}
    </button>
  );
}
