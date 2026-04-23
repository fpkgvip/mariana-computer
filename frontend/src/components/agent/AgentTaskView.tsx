import { useEffect, useMemo, useRef, useState } from "react";
import { AgentPlanCard, type AgentPlanStep } from "./AgentPlanCard";
import { AgentProgress } from "./AgentProgress";
import { TerminalOutput, type TerminalLine } from "./TerminalOutput";
import { WorkspaceSidebar } from "./WorkspaceSidebar";

interface AgentEvent {
  id?: string;
  task_id?: string;
  type: string;
  data?: Record<string, unknown>;
  ts?: number;
  replay?: boolean;
}

export interface AgentTaskViewProps {
  taskId: string;
  userId: string;
  apiUrl: string;
  /** Async accessor for a fresh bearer token. Avoids passing stale values. */
  getToken: () => Promise<string | null>;
  goal: string;
  /** Called when the task reaches a terminal state (DONE/FAILED/HALTED). */
  onTerminal?: (state: string) => void;
}

const MAX_LOG_LINES = 2000;

/**
 * Live view of a running agent task.
 * Consumes the SSE stream at /api/agent/{task_id}/stream with ?token=,
 * reconstructs plan + progress + terminal output from events.
 */
export function AgentTaskView({
  taskId,
  userId,
  apiUrl,
  getToken,
  goal,
  onTerminal,
}: AgentTaskViewProps) {
  const [token, setToken] = useState<string | null>(null);
  const [steps, setSteps] = useState<AgentPlanStep[]>([]);
  const [currentStepIndex, setCurrentStepIndex] = useState<number>(-1);
  const [state, setState] = useState<string>("PLAN");
  const [status, setStatus] = useState<string>("QUEUED");
  const [fixAttempts, setFixAttempts] = useState<number>(0);
  const [replans, setReplans] = useState<number>(0);
  const [lines, setLines] = useState<TerminalLine[]>([]);
  const [startedAt, setStartedAt] = useState<number>(Date.now());
  const [elapsed, setElapsed] = useState<number>(0);
  const [workspaceRefreshTick, setWorkspaceRefreshTick] = useState<number>(0);

  const esRef = useRef<EventSource | null>(null);
  const lineCounter = useRef(0);

  const pushLine = (kind: TerminalLine["kind"], text: string) => {
    lineCounter.current += 1;
    setLines((prev) => {
      const id = `ln-${lineCounter.current}`;
      const next = [...prev, { id, kind, text, ts: Date.now() }];
      if (next.length > MAX_LOG_LINES) return next.slice(-MAX_LOG_LINES);
      return next;
    });
  };

  // Fetch a fresh token on mount.
  useEffect(() => {
    let cancelled = false;
    getToken().then((t) => {
      if (!cancelled) setToken(t);
    });
    return () => {
      cancelled = true;
    };
  }, [getToken]);

  // SSE subscription (waits for token)
  useEffect(() => {
    if (!token) return;
    const url = `${apiUrl}/api/agent/${encodeURIComponent(taskId)}/stream?token=${encodeURIComponent(
      token,
    )}`;
    const es = new EventSource(url);
    esRef.current = es;
    setStartedAt(Date.now());

    const handleEvent = (evt: AgentEvent) => {
      const t = evt.type;
      const d = (evt.data ?? {}) as Record<string, unknown>;
      switch (t) {
        case "task_created":
        case "task_queued":
          setStatus("QUEUED");
          pushLine("info", `Task ${taskId.slice(0, 8)} queued.`);
          break;
        case "state_change":
          if (typeof d.to === "string") setState(d.to.toUpperCase());
          pushLine("info", `State → ${String(d.to ?? "?").toUpperCase()}`);
          break;
        case "plan_built":
        case "plan_updated": {
          const rawSteps = Array.isArray(d.steps) ? (d.steps as Record<string, unknown>[]) : [];
          const mapped: AgentPlanStep[] = rawSteps.map((s, i) => ({
            id: (s.id as string) || `step-${i}`,
            description: (s.description as string) || (s.goal as string) || `Step ${i + 1}`,
            tool: (s.tool as string) || undefined,
          }));
          setSteps(mapped);
          if (t === "plan_updated") setReplans((r) => r + 1);
          pushLine("info", `Plan: ${mapped.length} steps.`);
          break;
        }
        case "step_start": {
          const idx =
            typeof d.index === "number" ? (d.index as number) : typeof d.step_index === "number" ? (d.step_index as number) : -1;
          if (idx >= 0) setCurrentStepIndex(idx);
          pushLine("cmd", String(d.description ?? d.tool ?? "step started"));
          break;
        }
        case "step_end":
        case "step_complete": {
          const idx =
            typeof d.index === "number" ? (d.index as number) : typeof d.step_index === "number" ? (d.step_index as number) : -1;
          if (idx >= 0) setCurrentStepIndex(idx + 1);
          break;
        }
        case "tool_call":
          pushLine("cmd", `${String(d.tool ?? "tool")}(${JSON.stringify(d.args ?? {}).slice(0, 240)})`);
          break;
        case "tool_stdout":
        case "stdout":
          if (typeof d.text === "string") pushLine("stdout", d.text);
          else if (typeof d.line === "string") pushLine("stdout", d.line);
          break;
        case "tool_stderr":
        case "stderr":
          if (typeof d.text === "string") pushLine("stderr", d.text);
          else if (typeof d.line === "string") pushLine("stderr", d.line);
          break;
        case "tool_result": {
          const preview =
            typeof d.preview === "string"
              ? d.preview
              : JSON.stringify(d.result ?? d.value ?? "").slice(0, 400);
          if (preview) pushLine("stdout", preview);
          if (d.file_written || d.path || d.artifact) {
            setWorkspaceRefreshTick((n) => n + 1);
          }
          break;
        }
        case "fix_attempt":
          setFixAttempts((n) => n + 1);
          pushLine("info", `Fix attempt (${String(d.reason ?? "")}).`);
          break;
        case "task_done":
        case "task_failed":
        case "task_halted":
        case "eof": {
          const final =
            t === "task_done"
              ? "DONE"
              : t === "task_failed"
              ? "FAILED"
              : t === "task_halted"
              ? "HALTED"
              : (String(d.state ?? "DONE")).toUpperCase();
          setStatus(final);
          setState(final);
          pushLine("info", `Task ${final}.`);
          setWorkspaceRefreshTick((n) => n + 1);
          if (onTerminal) onTerminal(final);
          break;
        }
        case "error":
          pushLine("error", String(d.message ?? d.error ?? "unknown error"));
          break;
        default:
          if (t && !evt.replay) {
            const payload = JSON.stringify(d).slice(0, 240);
            if (payload.length > 2) pushLine("info", `${t}: ${payload}`);
          }
      }
    };

    es.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data);
        handleEvent(parsed);
      } catch {
        // Ignore heartbeat / malformed frames silently.
      }
    };
    es.onerror = () => {
      pushLine("error", "Stream disconnected. Retrying…");
    };

    return () => {
      es.close();
      esRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, apiUrl, token]);

  // Elapsed timer
  useEffect(() => {
    const terminal = ["DONE", "FAILED", "HALTED"].includes(status);
    if (terminal) return;
    const iv = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    return () => clearInterval(iv);
  }, [startedAt, status]);

  const handleStop = async () => {
    try {
      const freshToken = (await getToken()) ?? token;
      await fetch(`${apiUrl}/api/agent/${encodeURIComponent(taskId)}/stop`, {
        method: "POST",
        headers: freshToken ? { Authorization: `Bearer ${freshToken}` } : {},
      });
      pushLine("info", "Stop signal sent.");
    } catch (e) {
      pushLine("error", `Failed to send stop: ${String(e)}`);
    }
  };

  const totalSteps = useMemo(() => steps.length, [steps]);

  return (
    <div className="mt-3 grid grid-cols-1 lg:grid-cols-[1fr_280px] gap-4">
      <div className="min-w-0">
        <AgentPlanCard
          goal={goal}
          steps={steps}
          currentStepIndex={currentStepIndex}
          status={status}
          onCancel={handleStop}
        />
        <AgentProgress
          state={state}
          currentStepIndex={currentStepIndex}
          totalSteps={totalSteps}
          fixAttempts={fixAttempts}
          replans={replans}
          elapsedSeconds={elapsed}
        />
        <TerminalOutput lines={lines} height="360px" />
      </div>
      <WorkspaceSidebar
        apiUrl={apiUrl}
        getToken={getToken}
        userId={userId}
        refreshTick={workspaceRefreshTick}
      />
    </div>
  );
}
