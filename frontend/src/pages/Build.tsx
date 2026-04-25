/**
 * /build — Deft Studio
 *
 * Two modes:
 *   • IDLE — no active task. A centered prompt bar (the moment of intent),
 *     plus a preflight card showing the quote and tier picker.
 *   • LIVE — split pane. Left = LiveCanvas (plan, activity, artifacts).
 *     Right = PreviewPane iframe pointed at the deployed preview URL.
 *     Header offers "New run" + cancel; sidebar lists past projects.
 *
 * The split is the product. The receipt is the URL.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, Plus } from "lucide-react";
import { Navbar } from "@/components/Navbar";
import { PromptBar } from "@/components/deft/PromptBar";
import { PreflightCard } from "@/components/deft/PreflightCard";
import { LiveCanvas } from "@/components/deft/LiveCanvas";
import { PreviewPane } from "@/components/deft/PreviewPane";
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
import { useVault } from "@/hooks/useVault";
import { VaultUnlockDialog } from "@/components/deft/VaultUnlockDialog";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { scanVaultRefs, resolveVaultRefs, VaultRefError } from "@/lib/vaultPromptScan";
import { track } from "@/lib/analytics";
import { BRAND } from "@/lib/brand";

const FIRST_PROMPT_FLAG = "deft.firstPromptSubmitted.v1";

const TERMINAL_STATES = new Set(["done", "completed", "failed", "stopped", "cancelled", "error"]);

export default function Build() {
  const { user, loading: authLoading } = useAuth();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const taskIdParam = params.get("task");
  const promptParam = params.get("prompt");

  const { balance, refetch: refetchBalance } = useCredits(0);

  const [draftPrompt, setDraftPrompt] = useState(promptParam ?? "");

  // Strip ?prompt=... from URL after seeding so reloading doesn't re-prefill.
  useEffect(() => {
    if (!promptParam) return;
    const next = new URLSearchParams(params);
    next.delete("prompt");
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [activeTaskId, setActiveTaskId] = useState<string | null>(taskIdParam);
  const [task, setTask] = useState<AgentTaskState | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [connectionStatus, setConnectionStatus] = useState<"live" | "polling" | "closed">("closed");
  const [starting, setStarting] = useState(false);

  const eventSourceRef = useRef<EventSource | null>(null);
  const lastEventIdRef = useRef<number>(0);
  const pollTimerRef = useRef<number | null>(null);

  // F4 Vault wiring
  const vault = useVault();
  const [vaultUnlockOpen, setVaultUnlockOpen] = useState(false);
  const pendingSubmitRef = useRef<
    | { tier: ModelTier; ceiling: number; quote: QuoteResponse; refs: string[] }
    | null
  >(null);

  // Auth guard handled by ProtectedRoute, but be defensive
  useEffect(() => {
    if (!authLoading && !user) navigate("/login", { replace: true });
  }, [authLoading, user, navigate]);

  // Sync activeTaskId with URL
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

      try {
        const es = await openAgentStream(activeTaskId);
        eventSourceRef.current = es;
        setConnectionStatus("live");
        es.addEventListener("open", () => setConnectionStatus("live"));
        es.addEventListener("message", (msg) => handleStreamMessage((msg as MessageEvent).data));
        ["event", "task_state", "approval_requested", "step_started", "step_finished", "artifact"].forEach(
          (name) => es.addEventListener(name, (msg) => handleStreamMessage((msg as MessageEvent).data)),
        );
        es.addEventListener("error", () => {
          setConnectionStatus("polling");
          es.close();
          eventSourceRef.current = null;
          if (!pollTimerRef.current) {
            pollTimerRef.current = window.setInterval(() => void pollUpdates(), 4_000);
          }
        });
      } catch {
        setConnectionStatus("polling");
        if (!pollTimerRef.current) {
          pollTimerRef.current = window.setInterval(() => void pollUpdates(), 4_000);
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
        /* skip malformed */
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

  const launchRun = useCallback(
    async (
      tier: ModelTier,
      ceiling: number,
      quote: QuoteResponse,
      vaultEnv: Record<string, string> | undefined,
    ) => {
      setStarting(true);
      try {
        const resp = await startAgentRun({
          prompt: draftPrompt.trim(),
          tier,
          ceilingCredits: ceiling,
          vaultEnv,
        });
        try {
          if (typeof window !== "undefined" && !window.localStorage.getItem(FIRST_PROMPT_FLAG)) {
            window.localStorage.setItem(FIRST_PROMPT_FLAG, "1");
            track("first_prompt_submitted", {
              tier,
              ceiling,
              prompt_length: draftPrompt.trim().length,
            });
          }
        } catch {
          /* ignore */
        }
        refetchBalance();
        setParams((prev) => {
          const p = new URLSearchParams(prev);
          p.set("task", resp.task_id);
          return p;
        });
        toast.success("Run started");
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

  const handleSubmit = useCallback(
    async ({ tier, ceiling, quote }: { tier: ModelTier; ceiling: number; quote: QuoteResponse }) => {
      if (!draftPrompt.trim()) return;
      const { names } = scanVaultRefs(draftPrompt);
      let vaultEnv: Record<string, string> | undefined;
      if (names.length > 0) {
        if (!vault.exists) {
          toast.error("This prompt references vault secrets but no vault is set up.");
          navigate("/vault");
          return;
        }
        if (!vault.unlocked) {
          pendingSubmitRef.current = { tier, ceiling, quote, refs: names };
          setVaultUnlockOpen(true);
          return;
        }
        try {
          vaultEnv = await resolveVaultRefs(names, vault.decryptByName);
        } catch (e) {
          if (e instanceof VaultRefError) {
            toast.error(`${e.message}. Add it on the Vault page or remove the $${e.missingName} reference.`);
            return;
          }
          toast.error("Could not decrypt vault secrets");
          return;
        }
      }
      await launchRun(tier, ceiling, quote, vaultEnv);
    },
    [draftPrompt, launchRun, navigate, vault],
  );

  const onVaultUnlocked = useCallback(async () => {
    setVaultUnlockOpen(false);
    const pending = pendingSubmitRef.current;
    pendingSubmitRef.current = null;
    if (!pending) return;
    try {
      const env = await resolveVaultRefs(pending.refs, vault.decryptByName);
      await launchRun(pending.tier, pending.ceiling, pending.quote, env);
    } catch (e) {
      if (e instanceof VaultRefError) {
        toast.error(`${e.message}. Add it on the Vault page or remove the $${e.missingName} reference.`);
      } else {
        toast.error("Could not decrypt vault secrets");
      }
    }
  }, [launchRun, vault.decryptByName]);

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
      <div className="flex flex-1 overflow-hidden pt-16">
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

        <main className="relative flex flex-1 overflow-hidden">
          {activeTaskId && task ? (
            <LiveStudio
              task={task}
              events={events}
              connectionStatus={connectionStatus}
              onCancel={onCancel}
              onNew={newRun}
              taskId={activeTaskId}
            />
          ) : (
            <IdleStudio
              prompt={draftPrompt}
              onPromptChange={setDraftPrompt}
              onStart={handleSubmit}
              balance={balance}
              starting={starting}
            />
          )}
        </main>
      </div>

      <Dialog
        open={vaultUnlockOpen}
        onOpenChange={(o) => {
          if (!o) {
            pendingSubmitRef.current = null;
            setVaultUnlockOpen(false);
          }
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Unlock vault to use referenced secrets</DialogTitle>
            <DialogDescription>
              {pendingSubmitRef.current?.refs?.length
                ? `Your prompt references ${pendingSubmitRef.current.refs
                    .map((n) => `$${n}`)
                    .join(", ")}. Unlock to inject them as env vars for this run only.`
                : "Unlock your vault to continue."}
            </DialogDescription>
          </DialogHeader>
          <VaultUnlockDialog onUnlocked={onVaultUnlocked} bare />
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Idle: centered prompt + preflight (clean, focused, single column)
// ---------------------------------------------------------------------------

interface IdleStudioProps {
  prompt: string;
  onPromptChange: (v: string) => void;
  onStart: (params: { tier: ModelTier; ceiling: number; quote: QuoteResponse }) => void | Promise<void>;
  balance: number;
  starting: boolean;
}

function IdleStudio({ prompt, onPromptChange, onStart, balance, starting }: IdleStudioProps) {
  return (
    <div className="relative flex-1 overflow-auto">
      <div className="absolute inset-0 -z-0 bg-grid opacity-50" aria-hidden />
      <div className="absolute inset-0 -z-0 bg-vignette" aria-hidden />
      <div className="relative mx-auto flex w-full max-w-[860px] flex-col gap-5 px-6 py-12 md:py-16">
        <div className="text-center">
          <div className="mx-auto mb-4 inline-flex items-center gap-2 rounded-full border border-border/70 bg-surface-1/70 px-3 py-1 text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground backdrop-blur">
            <span className="size-1.5 rounded-full bg-deploy animate-pulse" />
            New run
          </div>
          <h1 className="text-balance text-3xl font-semibold tracking-[-0.02em] text-foreground sm:text-4xl">
            What should {BRAND.name} build?
          </h1>
          <p className="mx-auto mt-3 max-w-md text-[14.5px] leading-[1.6] text-muted-foreground">
            Plan, write, build, verify, ship — a complete app in one autonomous loop.
            Generation is free. Credits only spend when {BRAND.name} ships.
          </p>
        </div>

        <div className="rounded-2xl border border-border/80 bg-surface-1/85 p-4 shadow-elev-2 backdrop-blur-md focus-within:border-accent/60 focus-within:shadow-[0_0_0_4px_hsl(var(--accent)/0.10),0_18px_48px_-22px_hsl(var(--accent)/0.55)]">
          <PromptBar
            initialValue={prompt}
            onChange={onPromptChange}
            onSubmit={async (p) => onPromptChange(p)}
            busy={starting}
            placeholder="Build a habit tracker with a streak heatmap and Supabase auth…"
          />
        </div>

        {prompt.trim() && (
          <PreflightCard prompt={prompt} onStart={onStart} balance={balance} starting={starting} />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Live: split pane — LiveCanvas left, PreviewPane right
// ---------------------------------------------------------------------------

interface LiveStudioProps {
  task: AgentTaskState;
  events: AgentEvent[];
  connectionStatus: "live" | "polling" | "closed";
  onCancel: () => void;
  onNew: () => void;
  taskId: string;
}

function LiveStudio({ task, events, connectionStatus, onCancel, onNew, taskId }: LiveStudioProps) {
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      {/* Sub-bar */}
      <div className="flex items-center justify-between gap-3 border-b border-border/70 bg-surface-1/40 px-4 py-2.5 backdrop-blur">
        <button
          type="button"
          onClick={onNew}
          className="inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-surface-1 px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:border-accent/50 hover:text-foreground"
        >
          <ArrowLeft size={12} /> New run
        </button>

        <div className="hidden items-center gap-2 truncate text-[12px] text-muted-foreground sm:flex">
          <span className="truncate">{task.goal}</span>
        </div>

        <button
          type="button"
          onClick={onNew}
          className="inline-flex items-center gap-1.5 rounded-md bg-accent px-2.5 py-1.5 text-[12px] font-medium text-accent-foreground shadow-[0_4px_14px_-6px_hsl(var(--accent)/0.55)] transition-all hover:brightness-110"
        >
          <Plus size={12} /> Start another
        </button>
      </div>

      {/* Split */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 p-3 lg:grid-cols-[minmax(360px,1fr)_minmax(0,1.35fr)]">
        <div className="min-h-0">
          <LiveCanvas
            task={task}
            events={events}
            connectionStatus={connectionStatus}
            onCancel={onCancel}
            className="h-full"
          />
        </div>
        <div className="min-h-0">
          <PreviewPane taskId={taskId} task={task} events={events} className="h-full" />
        </div>
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
  if (merged.length > 1000) return merged.slice(merged.length - 1000);
  return merged;
}
