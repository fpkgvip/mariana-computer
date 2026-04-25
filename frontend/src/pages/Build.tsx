/**
 * /build — Deft Studio
 *
 * The core loop in one page:
 *   1. Prompt Bar (F1)
 *   2. Pre-flight Card (F2) — appears as soon as the prompt is non-empty
 *   3. Live Canvas (F3) — appears when an agent task is running
 *   4. Projects sidebar (F5) — list of past tasks
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ChevronLeft, FolderOpen, Plus, Search } from "lucide-react";
import { Navbar } from "@/components/Navbar";
import { PromptBar } from "@/components/deft/PromptBar";
import { PreflightCard } from "@/components/deft/PreflightCard";
import { LiveCanvas } from "@/components/deft/LiveCanvas";
import { ProjectsSidebar } from "@/components/deft/ProjectsSidebar";
import { useCredits, CREDITS_CHANGED_EVENT } from "@/hooks/useCredits";
import { useAuth } from "@/contexts/AuthContext";
import {
  startAgentRun,
  getAgentTask,
  getAgentEvents,
  openAgentStream,
  stopAgentRun,
  type AgentEvent,
  type AgentTaskState,
} from "@/lib/agentRunApi";
import { ApiError } from "@/lib/api";
import type { ModelTier, QuoteResponse } from "@/lib/agentApi";
import { toast } from "sonner";
import { cn } from "@/lib/utils";

const TERMINAL_STATES = new Set(["done", "completed", "failed", "stopped", "cancelled", "error"]);

export default function Build() {
  const { user, loading: authLoading } = useAuth();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const taskIdParam = params.get("task");

  const { balance, refetch: refetchBalance } = useCredits(0);

  const [draftPrompt, setDraftPrompt] = useState("");
  const [activeTaskId, setActiveTaskId] = useState<string | null>(taskIdParam);
  const [task, setTask] = useState<AgentTaskState | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [connectionStatus, setConnectionStatus] = useState<"live" | "polling" | "closed">("closed");
  const [starting, setStarting] = useState(false);

  const eventSourceRef = useRef<EventSource | null>(null);
  const lastEventIdRef = useRef<number>(0);
  const pollTimerRef = useRef<number | null>(null);

  // Auth guard handled by ProtectedRoute, but be defensive
  useEffect(() => {
    if (!authLoading && !user) navigate("/login", { replace: true });
  }, [authLoading, user, navigate]);

  // Sync activeTaskId with URL ?task=
  useEffect(() => {
    setActiveTaskId(taskIdParam);
  }, [taskIdParam]);

  // Initial REST fetch + SSE attach when activeTaskId changes
  useEffect(() => {
    setEvents([]);
    setTask(null);
    lastEventIdRef.current = 0;
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    if (pollTimerRef.current) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (!activeTaskId) {
      setConnectionStatus("closed");
      return;
    }

    const controller = new AbortController();

    (async () => {
      try {
        const t = await getAgentTask(activeTaskId, controller.signal);
        setTask(t);
        const evs = await getAgentEvents(activeTaskId, 0, 200, controller.signal);
        setEvents(evs.events);
        if (evs.events.length > 0) lastEventIdRef.current = evs.events[evs.events.length - 1].id;
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        const msg = err instanceof ApiError ? err.message : "Could not load task";
        toast.error(msg);
        return;
      }

      // Open SSE
      try {
        const es = await openAgentStream(activeTaskId);
        eventSourceRef.current = es;
        setConnectionStatus("live");

        es.addEventListener("open", () => setConnectionStatus("live"));

        es.addEventListener("message", (msg) => {
          handleStreamMessage((msg as MessageEvent).data);
        });

        // Custom named events the backend may emit
        ["event", "task_state", "approval_requested", "step_started", "step_finished", "artifact"].forEach(
          (name) => {
            es.addEventListener(name, (msg) => handleStreamMessage((msg as MessageEvent).data));
          },
        );

        es.addEventListener("error", () => {
          setConnectionStatus("polling");
          es.close();
          eventSourceRef.current = null;
          // Fallback poll every 4s
          if (!pollTimerRef.current) {
            pollTimerRef.current = window.setInterval(() => {
              void pollUpdates();
            }, 4_000);
          }
        });
      } catch (err) {
        // SSE setup failed — fall back to polling
        setConnectionStatus("polling");
        if (!pollTimerRef.current) {
          pollTimerRef.current = window.setInterval(() => {
            void pollUpdates();
          }, 4_000);
        }
      }
    })();

    async function pollUpdates() {
      if (!activeTaskId) return;
      try {
        const t = await getAgentTask(activeTaskId);
        setTask(t);
        const evs = await getAgentEvents(activeTaskId, lastEventIdRef.current, 200);
        if (evs.events.length > 0) {
          setEvents((prev) => mergeEvents(prev, evs.events));
          lastEventIdRef.current = evs.events[evs.events.length - 1].id;
        }
        if (TERMINAL_STATES.has(t.state)) {
          if (pollTimerRef.current) {
            window.clearInterval(pollTimerRef.current);
            pollTimerRef.current = null;
          }
          setConnectionStatus("closed");
          window.dispatchEvent(new CustomEvent(CREDITS_CHANGED_EVENT));
        }
      } catch {
        /* swallow transient errors */
      }
    }

    function handleStreamMessage(raw: string) {
      try {
        const data = JSON.parse(raw);
        // Server emits two payload shapes: an event row, or a task snapshot.
        if (data && typeof data === "object" && "event_type" in data && "id" in data) {
          const ev = data as AgentEvent;
          setEvents((prev) => mergeEvents(prev, [ev]));
          if (typeof ev.id === "number" && ev.id > lastEventIdRef.current) lastEventIdRef.current = ev.id;
          if (ev.event_type === "task_state" && ev.payload) {
            setTask((cur) => (cur ? { ...cur, ...(ev.payload as Partial<AgentTaskState>) } : cur));
          }
        } else if (data && typeof data === "object" && "state" in data && "id" in data) {
          setTask(data as AgentTaskState);
          if (TERMINAL_STATES.has((data as AgentTaskState).state)) {
            window.dispatchEvent(new CustomEvent(CREDITS_CHANGED_EVENT));
          }
        }
      } catch {
        /* skip malformed events */
      }
    }

    return () => {
      controller.abort();
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      if (pollTimerRef.current) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [activeTaskId]);

  const handleSubmit = useCallback(
    async ({ tier, ceiling, quote }: { tier: ModelTier; ceiling: number; quote: QuoteResponse }) => {
      if (!draftPrompt.trim()) return;
      setStarting(true);
      try {
        const resp = await startAgentRun({
          prompt: draftPrompt.trim(),
          tier,
          ceilingCredits: ceiling,
        });
        // Refresh balance (start may have reserved credits)
        refetchBalance();
        // Navigate to the running task
        setParams((prev) => {
          const p = new URLSearchParams(prev);
          p.set("task", resp.task_id);
          return p;
        });
        toast.success("Run started");
        // Brief: parent quote also returns approximate ceiling so we surface
        // ETA-from-quote (advisory) immediately while the canvas attaches.
        void quote;
      } catch (err) {
        const msg = err instanceof ApiError ? err.message : "Could not start run";
        toast.error(msg);
      } finally {
        setStarting(false);
      }
    },
    [draftPrompt, refetchBalance, setParams],
  );

  const onCancel = useCallback(async () => {
    if (!activeTaskId) return;
    try {
      await stopAgentRun(activeTaskId);
      toast("Stop requested");
      window.dispatchEvent(new CustomEvent(CREDITS_CHANGED_EVENT));
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Could not stop";
      toast.error(msg);
    }
  }, [activeTaskId]);

  const newRun = useCallback(() => {
    setDraftPrompt("");
    setParams((prev) => {
      const p = new URLSearchParams(prev);
      p.delete("task");
      return p;
    });
    setActiveTaskId(null);
  }, [setParams]);

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <Navbar />
      <div className="flex flex-1 overflow-hidden">
        <ProjectsSidebar
          activeTaskId={activeTaskId}
          onSelect={(id) => {
            setParams((prev) => {
              const p = new URLSearchParams(prev);
              p.set("task", id);
              return p;
            });
          }}
          onNew={newRun}
          balance={balance}
        />
        <main className="flex-1 overflow-auto">
          <div className="mx-auto flex w-full max-w-[1280px] flex-col gap-4 px-6 py-6">
            {activeTaskId && task ? (
              <>
                <div className="flex items-center justify-between">
                  <button
                    type="button"
                    onClick={newRun}
                    className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                  >
                    <ChevronLeft size={12} aria-hidden /> New run
                  </button>
                </div>
                <LiveCanvas
                  task={task}
                  events={events}
                  connectionStatus={connectionStatus}
                  onCancel={onCancel}
                />
              </>
            ) : (
              <>
                <div className="rounded-2xl border border-border bg-gradient-to-b from-card to-background p-6">
                  <div className="mb-4">
                    <h1 className="text-2xl font-semibold tracking-tight text-foreground">
                      What should Deft build?
                    </h1>
                    <p className="mt-1 text-sm text-muted-foreground">
                      Set a goal and a credit ceiling. Deft plans, executes, and hands you the receipt.
                    </p>
                  </div>
                  <PromptBar
                    initialValue={draftPrompt}
                    onChange={setDraftPrompt}
                    onSubmit={async (p) => setDraftPrompt(p)}
                    busy={starting}
                  />
                </div>
                {draftPrompt.trim() && (
                  <PreflightCard
                    prompt={draftPrompt}
                    onStart={handleSubmit}
                    balance={balance}
                    starting={starting}
                  />
                )}
              </>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}

function mergeEvents(prev: AgentEvent[], next: AgentEvent[]): AgentEvent[] {
  if (next.length === 0) return prev;
  const seen = new Set(prev.map((e) => e.id));
  const merged = [...prev];
  for (const e of next) {
    if (!seen.has(e.id)) {
      merged.push(e);
      seen.add(e.id);
    }
  }
  merged.sort((a, b) => a.id - b.id);
  // Cap at 1000 to avoid runaway memory in long sessions
  if (merged.length > 1000) return merged.slice(merged.length - 1000);
  return merged;
}
