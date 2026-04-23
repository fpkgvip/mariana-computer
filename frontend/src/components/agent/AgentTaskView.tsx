import { useEffect, useMemo, useRef, useState } from "react";
import { AgentPlanCard, type AgentPlanStep } from "./AgentPlanCard";
import { AgentProgress } from "./AgentProgress";
import { TerminalOutput, type TerminalLine } from "./TerminalOutput";
import { WorkspaceSidebar } from "./WorkspaceSidebar";

interface AgentEvent {
  event_type: string;
  task_id?: string;
  state?: string;
  step_id?: string;
  payload?: Record<string, unknown>;
  event_id?: number;
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
  const stepsRef = useRef<AgentPlanStep[]>([]);

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

    const findStepIdx = (stepId: unknown): number => {
      if (typeof stepId !== "string") return -1;
      // Use a ref-less approach: we read steps from state via closure.
      // React batches updates — for indexing purposes this is acceptable
      // because the plan rarely shifts beneath active indices.
      return stepsRef.current.findIndex((s) => s.id === stepId);
    };

    const handleEvent = (evt: AgentEvent) => {
      const t = evt.event_type;
      const d = (evt.payload ?? {}) as Record<string, unknown>;
      const stepId = evt.step_id ?? (d.step_id as string | undefined);
      switch (t) {
        case "state_change": {
          const to = typeof d.to === "string" ? d.to.toUpperCase() : "";
          if (to) {
            setState(to);
            if (["DONE", "FAILED", "HALTED"].includes(to)) {
              setStatus(to);
              setWorkspaceRefreshTick((n) => n + 1);
              if (onTerminal) onTerminal(to);
            } else if (to === "EXECUTE" || to === "TEST" || to === "DELIVER") {
              setStatus("RUNNING");
            }
          }
          pushLine("info", `state: ${String(d.from ?? "?")} → ${to || "?"}`);
          break;
        }
        case "plan_created": {
          const kind = String(d.kind ?? "initial");
          const rawSteps = Array.isArray(d.steps) ? (d.steps as Record<string, unknown>[]) : null;
          if (rawSteps) {
            const mapped: AgentPlanStep[] = rawSteps.map((s, i) => ({
              id: (s.id as string) || `step-${i}`,
              description: (s.title as string) || (s.description as string) || `Step ${i + 1}`,
              tool: (s.tool as string) || undefined,
            }));
            setSteps(mapped);
            stepsRef.current = mapped;
            pushLine("info", `Plan (${kind}): ${mapped.length} steps.`);
            if (kind === "replan") setReplans((r) => r + 1);
          } else if (d.step) {
            // Single-step fix — replace in-place by id.
            const s = d.step as Record<string, unknown>;
            const replaced: AgentPlanStep = {
              id: (s.id as string) || "",
              description: (s.title as string) || (s.description as string) || "",
              tool: (s.tool as string) || undefined,
            };
            setSteps((prev) => {
              const next = prev.map((p) => (p.id === replaced.id ? replaced : p));
              stepsRef.current = next;
              return next;
            });
            setFixAttempts((n) => n + 1);
            pushLine("info", `Fix applied to step ${replaced.id.slice(0, 8)}…`);
          }
          break;
        }
        case "step_started": {
          const idx = findStepIdx(stepId);
          if (idx >= 0) setCurrentStepIndex(idx);
          const tool = String(d.tool ?? "");
          const title = String(d.title ?? tool ?? "step");
          const params = d.params ? JSON.stringify(d.params).slice(0, 240) : "";
          pushLine("cmd", `${tool ? tool + ": " : ""}${title}${params ? " " + params : ""}`);
          break;
        }
        case "step_completed": {
          const idx = findStepIdx(stepId);
          if (idx >= 0) setCurrentStepIndex(idx + 1);
          const dur = typeof d.duration_ms === "number" ? ` (${Math.round((d.duration_ms as number) / 10) / 100}s)` : "";
          pushLine("info", `✓ step done${dur}`);
          break;
        }
        case "step_failed": {
          const err = String(d.error ?? "failed");
          pushLine("stderr", `step failed: ${err}`);
          break;
        }
        case "terminal_output": {
          const stdout = typeof d.stdout === "string" ? (d.stdout as string) : "";
          const stderr = typeof d.stderr === "string" ? (d.stderr as string) : "";
          const exitCode = d.exit_code;
          if (stdout) stdout.split(/\r?\n/).forEach((ln) => { if (ln) pushLine("stdout", ln); });
          if (stderr) stderr.split(/\r?\n/).forEach((ln) => { if (ln) pushLine("stderr", ln); });
          if (typeof exitCode === "number" && exitCode !== 0) {
            pushLine("stderr", `exit code: ${exitCode}`);
          }
          break;
        }
        case "artifact_created": {
          const name = String(d.name ?? d.workspace_path ?? "artifact");
          const size = typeof d.size === "number" ? ` (${d.size} B)` : "";
          pushLine("info", `artifact: ${name}${size}`);
          setWorkspaceRefreshTick((n) => n + 1);
          break;
        }
        case "delivered": {
          const ans = String(d.final_answer ?? "");
          if (ans) {
            ans.split(/\r?\n/).forEach((ln) => pushLine("info", ln));
          }
          setWorkspaceRefreshTick((n) => n + 1);
          break;
        }
        case "halted": {
          const reason = String(d.reason ?? "halted");
          pushLine("info", `halted: ${reason}`);
          setStatus("HALTED");
          setState("HALTED");
          if (onTerminal) onTerminal("HALTED");
          break;
        }
        case "error": {
          const phase = String(d.phase ?? "");
          const err = String(d.error ?? d.message ?? "unknown error");
          pushLine("error", `${phase ? phase + ": " : ""}${err}`);
          break;
        }
        case "eof":
          // Stream closed; no-op (handled by terminal state_change).
          break;
        default:
          if (t && !evt.replay) {
            const payload = JSON.stringify(d).slice(0, 200);
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
