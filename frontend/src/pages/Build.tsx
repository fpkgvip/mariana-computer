/**
 * /build — Deft Studio
 *
 * Three-zone shell:
 *   • ProjectsSidebar (left rail, slide-over below lg)
 *   • StudioHeader     (sticky strip: stage chip · title · credits · cancel)
 *   • Canvas           (IdleStudio when no task, LiveStudio when a run is active)
 *
 * The split is the product. The receipt is the URL.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { ProjectsSidebar } from "@/components/deft/ProjectsSidebar";
import { StudioFrame } from "@/components/deft/studio/StudioFrame";
import { StudioHeader } from "@/components/deft/studio/StudioHeader";
import { IdleStudio } from "@/components/deft/studio/IdleStudio";
import { LiveStudio } from "@/components/deft/studio/LiveStudio";
import {
  canCancelStage,
  creditsFromUsd,
  deriveStudioStage,
  durationSeconds,
} from "@/components/deft/studio/stage";
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
  const [cancelConfirmOpen, setCancelConfirmOpen] = useState(false);

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

  const confirmCancel = useCallback(async () => {
    if (!activeTaskId) return;
    setCancelConfirmOpen(false);
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

  // Derive header state once per render.
  const stage = useMemo(() => deriveStudioStage(task, events), [task, events]);
  const spentCredits = creditsFromUsd(task?.spent_usd);
  const ceilingCredits = creditsFromUsd(task?.budget_usd);
  const headerDuration = task ? durationSeconds(task) : undefined;
  const cancellable = Boolean(task) && canCancelStage(stage);

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <Navbar />
      <div className="flex flex-1 min-h-0 overflow-hidden pt-16">
        <StudioFrame
          projects={
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
          }
          header={
            <StudioHeader
              title={task?.goal ?? ""}
              stage={stage}
              spentCredits={task ? spentCredits : undefined}
              ceilingCredits={task ? ceilingCredits : undefined}
              durationSec={headerDuration}
              canCancel={cancellable}
              onCancel={() => setCancelConfirmOpen(true)}
              onNewRun={activeTaskId ? newRun : undefined}
            />
          }
        >
          {activeTaskId && task ? (
            <LiveStudio
              task={task}
              events={events}
              connectionStatus={connectionStatus}
              onCancel={() => setCancelConfirmOpen(true)}
              taskId={activeTaskId}
            />
          ) : (
            <IdleStudio
              prompt={draftPrompt}
              onPromptChange={setDraftPrompt}
              onStart={handleSubmit}
              balance={balance}
              starting={starting}
              unlimited={user?.role === "admin"}
            />
          )}
        </StudioFrame>
      </div>

      {/* Cancel confirmation */}
      <Dialog open={cancelConfirmOpen} onOpenChange={setCancelConfirmOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Cancel this run?</DialogTitle>
            <DialogDescription>
              Credits already burned will not be refunded. Pending steps stop within 5 seconds.
            </DialogDescription>
          </DialogHeader>
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setCancelConfirmOpen(false)}
              className="inline-flex items-center rounded-md border border-border px-3 py-1.5 text-[13px] font-medium text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
            >
              Keep running
            </button>
            <button
              type="button"
              onClick={confirmCancel}
              className="inline-flex items-center rounded-md bg-destructive px-3 py-1.5 text-[13px] font-medium text-destructive-foreground transition-opacity hover:opacity-90"
            >
              Cancel run
            </button>
          </div>
        </DialogContent>
      </Dialog>

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
