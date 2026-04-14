import { useState, useEffect, useRef, useCallback } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Link, useNavigate } from "react-router-dom";
import {
  Send,
  AlertTriangle,
  ChevronDown,
  Menu,
  X,
  Download,
  FileText,
  RefreshCw,
  Clock,
  Loader2,
} from "lucide-react";
import { toast } from "sonner";
import { supabase } from "@/lib/supabase";

/* ------------------------------------------------------------------ */
/*  Types                                                             */
/* ------------------------------------------------------------------ */

interface Message {
  role: "user" | "assistant" | "system";
  content: string;
  type?: "text" | "code" | "status" | "error";
  id?: string; // dedup key for status messages
}

type InvestigationStatus = "PENDING" | "RUNNING" | "COMPLETED" | "FAILED";

interface Investigation {
  task_id: string;
  topic: string;
  status: InvestigationStatus;
  created_at: string;
  duration_hours: number;
  budget_usd: number;
}

/** POST /api/investigations response */
interface CreateInvestigationResponse {
  task_id: string;
  status: string;
  message: string;
}

/** GET /api/investigations/{task_id} polling response */
interface InvestigationPollResponse {
  task_id: string;
  status: InvestigationStatus;
  topic?: string;
  findings?: string;
  status_message?: string;
  error?: string;
}

/* ------------------------------------------------------------------ */
/*  Constants                                                         */
/* ------------------------------------------------------------------ */

const API_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "";

interface DurationOption {
  value: string;
  label: string;
  timeLabel: string;
  hours: number; // midpoint used for budget and countdown
  warn: boolean;
}

const durationOptions: DurationOption[] = [
  { value: "quick", label: "Quick", timeLabel: "5–15 min", hours: 0.17, warn: false },
  { value: "deep", label: "Deep", timeLabel: "1–2 hours", hours: 1.5, warn: false },
  { value: "professional", label: "Professional", timeLabel: "6–12 hours", hours: 9, warn: true },
  { value: "flagship", label: "Flagship", timeLabel: "24–72 hours", hours: 48, warn: true },
  { value: "marathon", label: "Marathon", timeLabel: "3–5 days", hours: 96, warn: true },
  { value: "custom", label: "Custom", timeLabel: "", hours: 0, warn: false },
];

const STATUS_COLORS: Record<InvestigationStatus, string> = {
  PENDING: "bg-yellow-500/20 text-yellow-400 ring-yellow-500/30",
  RUNNING: "bg-blue-500/20 text-blue-400 ring-blue-500/30",
  COMPLETED: "bg-green-500/20 text-green-400 ring-green-500/30",
  FAILED: "bg-red-500/20 text-red-400 ring-red-500/30",
};

/* ------------------------------------------------------------------ */
/*  Helpers                                                           */
/* ------------------------------------------------------------------ */

async function getAccessToken(): Promise<string | null> {
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

/** Simple markdown-ish rendering: code blocks, bold, italic, newlines */
function renderMarkdown(text: string): string {
  let html = text
    // Escape HTML
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    // Fenced code blocks
    .replace(/```(\w*)\n([\s\S]*?)```/g, (_m, _lang, code) => {
      return `<pre class="my-2 rounded-md bg-zinc-900 px-4 py-3 text-xs leading-relaxed overflow-x-auto"><code>${code.trim()}</code></pre>`;
    })
    // Inline code
    .replace(/`([^`]+)`/g, '<code class="rounded bg-zinc-800 px-1.5 py-0.5 text-xs">$1</code>')
    // Bold
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    // Italic
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    // Newlines
    .replace(/\n/g, "<br />");
  return html;
}

function formatDuration(hours: number): string {
  if (hours < 1) {
    const mins = Math.round(hours * 60);
    return `${mins} min`;
  }
  if (hours < 24) {
    const h = Math.floor(hours);
    const m = Math.round((hours - h) * 60);
    return m > 0 ? `${h}h ${m}m` : `${h}h`;
  }
  const days = Math.round(hours / 24 * 10) / 10;
  return `${days} days`;
}

function formatCountdown(secondsRemaining: number): string {
  if (secondsRemaining <= 0) return "0:00";
  const h = Math.floor(secondsRemaining / 3600);
  const m = Math.floor((secondsRemaining % 3600) / 60);
  const s = Math.floor(secondsRemaining % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/* ------------------------------------------------------------------ */
/*  Component                                                         */
/* ------------------------------------------------------------------ */

export default function Chat() {
  const { user } = useAuth();
  const navigate = useNavigate();

  // Messages for the currently viewed investigation
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [duration, setDuration] = useState("deep");
  const [customHours, setCustomHours] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [retryPayload, setRetryPayload] = useState<{ topic: string } | null>(null);

  // Investigation management
  const [investigations, setInvestigations] = useState<Investigation[]>([]);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);

  // Timer state
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startTimeRef = useRef<number | null>(null);

  // SSE / polling refs
  const eventSourceRef = useRef<EventSource | null>(null);
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const seenStatusIds = useRef<Set<string>>(new Set());

  // Auto-scroll
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);

  // Per-investigation message store
  const messageStoreRef = useRef<Record<string, Message[]>>({});

  /* ---------------------------------------------------------------- */
  /*  Auth guard                                                      */
  /* ---------------------------------------------------------------- */

  useEffect(() => {
    if (!user) navigate("/login");
  }, [user, navigate]);

  /* ---------------------------------------------------------------- */
  /*  Load investigations from Supabase on mount                      */
  /* ---------------------------------------------------------------- */

  useEffect(() => {
    if (!user) return;
    const loadInvestigations = async () => {
      const { data, error } = await supabase
        .from("investigations")
        .select("task_id, topic, status, created_at, duration_hours, budget_usd")
        .order("created_at", { ascending: false });
      if (error) {
        console.error("[Chat] Failed to load investigations:", error.message);
        return;
      }
      if (data && data.length > 0) {
        setInvestigations(data as Investigation[]);
      }
    };
    loadInvestigations();
  }, [user]);

  /* ---------------------------------------------------------------- */
  /*  Auto-scroll on new messages                                     */
  /* ---------------------------------------------------------------- */

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  /* ---------------------------------------------------------------- */
  /*  Cleanup on unmount                                              */
  /* ---------------------------------------------------------------- */

  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
      if (eventSourceRef.current) eventSourceRef.current.close();
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  /* ---------------------------------------------------------------- */
  /*  Timer management                                                */
  /* ---------------------------------------------------------------- */

  const startTimer = useCallback(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    startTimeRef.current = Date.now();
    setElapsedSeconds(0);
    timerRef.current = setInterval(() => {
      if (startTimeRef.current) {
        setElapsedSeconds(Math.floor((Date.now() - startTimeRef.current) / 1000));
      }
    }, 1000);
  }, []);

  const stopTimer = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    startTimeRef.current = null;
  }, []);

  /* ---------------------------------------------------------------- */
  /*  Deduped message appender                                        */
  /* ---------------------------------------------------------------- */

  const appendMessage = useCallback((msg: Message) => {
    const msgId = msg.id || `${msg.role}-${msg.content}`;
    if (msg.type === "status" && seenStatusIds.current.has(msgId)) return;
    if (msg.type === "status") seenStatusIds.current.add(msgId);
    setMessages((prev) => [...prev, msg]);
  }, []);

  /* ---------------------------------------------------------------- */
  /*  Update investigation status locally and in Supabase             */
  /* ---------------------------------------------------------------- */

  const updateInvestigationStatus = useCallback(
    async (taskId: string, status: InvestigationStatus) => {
      setInvestigations((prev) =>
        prev.map((inv) => (inv.task_id === taskId ? { ...inv, status } : inv))
      );
      await supabase
        .from("investigations")
        .update({ status })
        .eq("task_id", taskId)
        .then(({ error }) => {
          if (error) console.error("[Chat] Failed to update investigation status:", error.message);
        });
    },
    []
  );

  /* ---------------------------------------------------------------- */
  /*  Stop all real-time connections                                   */
  /* ---------------------------------------------------------------- */

  const stopAllConnections = useCallback(() => {
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    setIsSending(false);
    stopTimer();
  }, [stopTimer]);

  /* ---------------------------------------------------------------- */
  /*  Polling fallback                                                */
  /* ---------------------------------------------------------------- */

  const startPolling = useCallback(
    (taskId: string, token: string) => {
      const poll = async () => {
        try {
          const res = await fetch(`${API_URL}/api/investigations/${taskId}`, {
            headers: {
              Authorization: `Bearer ${token}`,
              "Content-Type": "application/json",
            },
          });

          if (res.status === 401) {
            stopAllConnections();
            toast.error("Session expired", { description: "Please sign in again." });
            navigate("/login");
            return;
          }

          if (res.status === 429) {
            const retryAfter = res.headers.get("Retry-After") || "30";
            appendMessage({
              role: "system",
              content: `Rate limited. Retrying in ${retryAfter} seconds...`,
              type: "status",
              id: `rate-limit-${Date.now()}`,
            });
            return;
          }

          if (!res.ok) return;

          const data: InvestigationPollResponse = await res.json();

          if (data.status_message) {
            appendMessage({
              role: "system",
              content: data.status_message,
              type: "status",
              id: `poll-${data.status_message}`,
            });
          }

          if (data.status === "RUNNING") {
            updateInvestigationStatus(taskId, "RUNNING");
          }

          if (data.status === "COMPLETED") {
            updateInvestigationStatus(taskId, "COMPLETED");
            if (data.findings) {
              appendMessage({
                role: "assistant",
                content: data.findings,
                type: "text",
              });
            }
            appendMessage({
              role: "system",
              content: "Investigation complete. Reports are ready for download.",
              type: "status",
              id: `completed-${taskId}`,
            });
            stopAllConnections();
          } else if (data.status === "FAILED") {
            updateInvestigationStatus(taskId, "FAILED");
            const errMsg = data.error ?? "The investigation failed. Please try again.";
            appendMessage({
              role: "assistant",
              content: errMsg,
              type: "text",
            });
            toast.error("Investigation failed", { description: errMsg });
            stopAllConnections();
          }
        } catch (err) {
          console.error("[Chat] Polling error:", err);
        }
      };

      poll();
      pollIntervalRef.current = setInterval(poll, 5000);
    },
    [appendMessage, navigate, stopAllConnections, updateInvestigationStatus]
  );

  /* ---------------------------------------------------------------- */
  /*  SSE streaming                                                   */
  /* ---------------------------------------------------------------- */

  const startSSE = useCallback(
    (taskId: string, token: string) => {
      const url = `${API_URL}/api/investigations/${taskId}/logs`;

      try {
        const es = new EventSource(url);
        eventSourceRef.current = es;

        es.onmessage = (event) => {
          try {
            const parsed = JSON.parse(event.data);
            const content = parsed.message || parsed.content || parsed.data || event.data;
            const status = parsed.status as InvestigationStatus | undefined;

            appendMessage({
              role: "system",
              content: String(content),
              type: "status",
              id: `sse-${event.lastEventId || content}`,
            });

            if (status === "RUNNING") {
              updateInvestigationStatus(taskId, "RUNNING");
            }

            if (status === "COMPLETED") {
              updateInvestigationStatus(taskId, "COMPLETED");
              if (parsed.findings) {
                appendMessage({
                  role: "assistant",
                  content: parsed.findings,
                  type: "text",
                });
              }
              appendMessage({
                role: "system",
                content: "Investigation complete. Reports are ready for download.",
                type: "status",
                id: `completed-${taskId}`,
              });
              stopAllConnections();
            } else if (status === "FAILED") {
              updateInvestigationStatus(taskId, "FAILED");
              appendMessage({
                role: "assistant",
                content: parsed.error || "The investigation failed.",
                type: "text",
              });
              stopAllConnections();
            }
          } catch {
            // Plain text SSE event
            appendMessage({
              role: "system",
              content: event.data,
              type: "status",
              id: `sse-${event.data}`,
            });
          }
        };

        es.onerror = () => {
          console.warn("[Chat] SSE connection error, falling back to polling.");
          es.close();
          eventSourceRef.current = null;
          // Fall back to polling
          startPolling(taskId, token);
        };
      } catch {
        console.warn("[Chat] SSE not available, using polling.");
        startPolling(taskId, token);
      }
    },
    [appendMessage, startPolling, stopAllConnections, updateInvestigationStatus]
  );

  /* ---------------------------------------------------------------- */
  /*  Resolve effective duration hours                                */
  /* ---------------------------------------------------------------- */

  const getEffectiveHours = useCallback((): number => {
    if (duration === "custom") {
      const parsed = parseFloat(customHours);
      return isNaN(parsed) || parsed <= 0 ? 1 : parsed;
    }
    const opt = durationOptions.find((d) => d.value === duration);
    return opt?.hours ?? 1.5;
  }, [duration, customHours]);

  /* ---------------------------------------------------------------- */
  /*  Switch active investigation                                     */
  /* ---------------------------------------------------------------- */

  const switchInvestigation = useCallback(
    (taskId: string) => {
      // Save current messages
      if (activeTaskId && messages.length > 0) {
        messageStoreRef.current[activeTaskId] = [...messages];
      }

      // Stop any active connections
      stopAllConnections();
      seenStatusIds.current.clear();

      // Load stored messages for the target investigation
      const stored = messageStoreRef.current[taskId];
      setMessages(stored || []);
      setActiveTaskId(taskId);
      setSidebarOpen(false);

      // If investigation is still running, reconnect SSE
      const inv = investigations.find((i) => i.task_id === taskId);
      if (inv && (inv.status === "RUNNING" || inv.status === "PENDING")) {
        setIsSending(true);
        // Re-seed seen IDs from stored messages
        if (stored) {
          stored.forEach((m) => {
            if (m.id) seenStatusIds.current.add(m.id);
          });
        }
        getAccessToken().then((token) => {
          if (token) {
            startTimer();
            startSSE(taskId, token);
          }
        });
      }
    },
    [activeTaskId, messages, investigations, stopAllConnections, startSSE, startTimer]
  );

  /* ---------------------------------------------------------------- */
  /*  Send investigation                                              */
  /* ---------------------------------------------------------------- */

  const handleSend = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const topic = retryPayload?.topic || input.trim();
    if (!topic || isSending) return;

    setRetryPayload(null);
    setInput("");
    setIsSending(true);

    // Save current investigation messages before starting new
    if (activeTaskId && messages.length > 0) {
      messageStoreRef.current[activeTaskId] = [...messages];
    }

    // Reset state for new investigation
    seenStatusIds.current.clear();
    stopAllConnections();

    const newMessages: Message[] = [
      { role: "user", content: topic, type: "text" },
    ];
    setMessages(newMessages);

    // Get auth token
    const token = await getAccessToken();
    if (!token) {
      toast.error("Not authenticated", {
        description: "Please sign in to run an investigation.",
      });
      setIsSending(false);
      navigate("/login");
      return;
    }

    // Show initializing status
    const initMsg: Message = {
      role: "system",
      content: "Initializing research environment...",
      type: "status",
      id: "init",
    };
    seenStatusIds.current.add("init");
    setMessages((prev) => [...prev, initMsg]);

    const effectiveHours = getEffectiveHours();
    const budgetUsd = Math.max(1, Math.round(effectiveHours * 5));

    try {
      const res = await fetch(`${API_URL}/api/investigations`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          topic,
          budget_usd: budgetUsd,
          duration_hours: effectiveHours,
        }),
      });

      if (res.status === 401) {
        setIsSending(false);
        toast.error("Session expired", { description: "Please sign in again." });
        navigate("/login");
        return;
      }

      if (res.status === 429) {
        const retryAfter = res.headers.get("Retry-After") || "30";
        setIsSending(false);
        setRetryPayload({ topic });
        appendMessage({
          role: "system",
          content: `Rate limited. Please retry in ${retryAfter} seconds.`,
          type: "error",
          id: `rate-limit-${Date.now()}`,
        });
        return;
      }

      if (!res.ok) {
        const errorText = await res.text().catch(() => res.statusText);
        throw new Error(`HTTP ${res.status}: ${errorText}`);
      }

      const data: CreateInvestigationResponse = await res.json();
      const taskId = data.task_id;

      // Create investigation record
      const newInvestigation: Investigation = {
        task_id: taskId,
        topic,
        status: "PENDING",
        created_at: new Date().toISOString(),
        duration_hours: effectiveHours,
        budget_usd: budgetUsd,
      };

      setInvestigations((prev) => [newInvestigation, ...prev]);
      setActiveTaskId(taskId);

      // Persist to Supabase
      supabase
        .from("investigations")
        .insert({
          task_id: taskId,
          topic,
          status: "PENDING",
          duration_hours: effectiveHours,
          budget_usd: budgetUsd,
          user_id: user?.id,
        })
        .then(({ error }) => {
          if (error) console.error("[Chat] Failed to persist investigation:", error.message);
        });

      appendMessage({
        role: "system",
        content: `Investigation started (ID: ${taskId}). Estimated duration: ${formatDuration(effectiveHours)}. Monitoring progress...`,
        type: "status",
        id: `started-${taskId}`,
      });

      // Start timer and SSE
      startTimer();
      startSSE(taskId, token);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Unknown error";
      toast.error("Failed to start investigation", { description: message });
      setRetryPayload({ topic });
      appendMessage({
        role: "system",
        content: `Could not start the investigation: ${message}`,
        type: "error",
        id: `error-${Date.now()}`,
      });
      setIsSending(false);
    }
  };

  /* ---------------------------------------------------------------- */
  /*  Retry handler                                                   */
  /* ---------------------------------------------------------------- */

  const handleRetry = () => {
    if (retryPayload) {
      handleSend();
    }
  };

  /* ---------------------------------------------------------------- */
  /*  Derived state                                                   */
  /* ---------------------------------------------------------------- */

  const selectedDuration = durationOptions.find((d) => d.value === duration);
  const effectiveHours = getEffectiveHours();
  const totalSeconds = effectiveHours * 3600;
  const progressPercent = totalSeconds > 0 ? Math.min(100, (elapsedSeconds / totalSeconds) * 100) : 0;
  const secondsRemaining = Math.max(0, totalSeconds - elapsedSeconds);

  const activeInvestigation = investigations.find((i) => i.task_id === activeTaskId);
  const isRunning = activeInvestigation?.status === "RUNNING" || activeInvestigation?.status === "PENDING";
  const isCompleted = activeInvestigation?.status === "COMPLETED";

  if (!user) return null;

  return (
    <div className="flex h-screen bg-background">
      {/* Mobile sidebar overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/40 md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* ============================================================ */}
      {/*  Sidebar                                                     */}
      {/* ============================================================ */}
      <div
        className={`fixed inset-y-0 left-0 z-50 w-64 flex-col border-r border-border bg-card transition-transform duration-300 md:relative md:z-auto md:flex md:translate-x-0 ${
          sidebarOpen ? "flex translate-x-0" : "hidden -translate-x-full"
        }`}
      >
        <div className="flex h-16 items-center justify-between border-b border-border px-5">
          <Link to="/" className="font-serif text-sm font-semibold text-foreground">
            Mariana
          </Link>
          <button
            onClick={() => setSidebarOpen(false)}
            className="md:hidden text-muted-foreground"
          >
            <X size={18} />
          </button>
        </div>

        {/* New investigation button */}
        <div className="px-4 pt-4">
          <button
            onClick={() => {
              if (activeTaskId && messages.length > 0) {
                messageStoreRef.current[activeTaskId] = [...messages];
              }
              stopAllConnections();
              setActiveTaskId(null);
              setMessages([]);
              seenStatusIds.current.clear();
              setSidebarOpen(false);
            }}
            className="w-full rounded-md border border-border px-3 py-2 text-xs text-foreground hover:bg-secondary transition-colors"
          >
            + New Investigation
          </button>
        </div>

        {/* Investigation list */}
        <div className="flex-1 overflow-y-auto px-4 py-4">
          <p className="mb-2 text-[10px] font-medium uppercase tracking-[0.15em] text-muted-foreground">
            Investigations
          </p>
          <div className="space-y-1">
            {investigations.length === 0 && (
              <p className="text-xs text-muted-foreground/60 py-2">No investigations yet</p>
            )}
            {investigations.map((inv) => (
              <button
                key={inv.task_id}
                onClick={() => switchInvestigation(inv.task_id)}
                className={`w-full rounded-md px-3 py-2 text-left text-xs transition-colors ${
                  activeTaskId === inv.task_id
                    ? "bg-secondary text-foreground"
                    : "text-muted-foreground hover:bg-secondary/50 hover:text-foreground"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span
                    className={`inline-flex shrink-0 items-center rounded-full px-1.5 py-0.5 text-[9px] font-medium ring-1 ring-inset ${
                      STATUS_COLORS[inv.status]
                    }`}
                  >
                    {inv.status}
                  </span>
                  <span className="truncate">{inv.topic}</span>
                </div>
              </button>
            ))}
          </div>
        </div>

        {/* User info */}
        <div className="border-t border-border px-4 py-3">
          <div className="text-xs text-muted-foreground">
            <span className="font-medium text-foreground">
              ${(user.tokens / 10).toFixed(2)}
            </span>{" "}
            credit remaining
          </div>
          <p className="mt-1 text-[10px] text-muted-foreground">
            {user.name} · {user.email}
          </p>
        </div>
      </div>

      {/* ============================================================ */}
      {/*  Main content                                                */}
      {/* ============================================================ */}
      <div className="flex flex-1 flex-col">
        {/* Header */}
        <div className="flex h-16 items-center justify-between border-b border-border px-4 sm:px-6">
          <div className="flex items-center gap-3">
            <button
              onClick={() => setSidebarOpen(true)}
              className="md:hidden text-foreground"
              aria-label="Open sidebar"
            >
              <Menu size={20} />
            </button>
            <Link
              to="/"
              className="font-serif text-sm font-semibold text-foreground md:hidden"
            >
              Mariana
            </Link>
            <span className="hidden text-xs text-muted-foreground md:inline">
              Mariana Computer
            </span>
          </div>

          {/* Running indicator + timer */}
          <div className="flex items-center gap-3">
            {isRunning && isSending && (
              <div className="flex items-center gap-2 text-xs text-blue-400">
                <span className="relative flex h-2 w-2">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-400 opacity-75" />
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-blue-500" />
                </span>
                <Clock size={12} />
                <span className="font-mono">{formatCountdown(secondsRemaining)}</span>
              </div>
            )}
            <div className="text-xs text-muted-foreground md:hidden">
              ${(user.tokens / 10).toFixed(2)} credit
            </div>
          </div>
        </div>

        {/* Progress bar */}
        {isRunning && isSending && (
          <div className="h-1 w-full bg-zinc-800">
            <div
              className="h-full bg-blue-500 transition-all duration-1000 ease-linear"
              style={{ width: `${progressPercent}%` }}
            />
          </div>
        )}

        {/* ---------------------------------------------------------- */}
        {/*  Messages area                                             */}
        {/* ---------------------------------------------------------- */}
        <div
          ref={messagesContainerRef}
          className="flex-1 overflow-y-auto px-4 py-6 sm:px-6"
        >
          <div className="mx-auto max-w-2xl space-y-4">
            {/* Empty state */}
            {messages.length === 0 && !isSending && (
              <div className="flex flex-col items-center justify-center py-24 text-center">
                <h2 className="font-serif text-xl font-semibold text-foreground mb-2">
                  What would you like Mariana to investigate?
                </h2>
                <p className="text-sm text-muted-foreground max-w-md">
                  Describe a financial research question, company analysis, or market investigation.
                  Mariana will autonomously research and compile a comprehensive report.
                </p>
              </div>
            )}

            {/* Messages */}
            {messages.filter(Boolean).map((msg, i) => (
              <div
                key={i}
                className="animate-fade-in"
                style={{ animationDelay: `${Math.min(i * 30, 300)}ms` }}
              >
                {msg.role === "user" ? (
                  <div className="flex justify-end">
                    <div className="max-w-[85%] rounded-lg bg-primary/5 px-4 py-3 text-sm leading-relaxed text-foreground ring-1 ring-primary/10 sm:max-w-md">
                      {msg.content}
                    </div>
                  </div>
                ) : msg.type === "status" ? (
                  <div className="border-l-2 border-accent/40 pl-4 py-2">
                    <pre className="font-mono text-xs leading-6 text-muted-foreground whitespace-pre-wrap break-words">
                      {msg.content}
                    </pre>
                  </div>
                ) : msg.type === "error" ? (
                  <div className="border-l-2 border-red-500/40 pl-4 py-2">
                    <div className="flex items-start gap-2">
                      <AlertTriangle size={14} className="mt-0.5 shrink-0 text-red-400" />
                      <pre className="font-mono text-xs leading-6 text-red-400 whitespace-pre-wrap break-words">
                        {msg.content}
                      </pre>
                    </div>
                    {retryPayload && (
                      <button
                        onClick={handleRetry}
                        className="mt-2 ml-5 flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs text-foreground hover:bg-secondary transition-colors"
                      >
                        <RefreshCw size={12} />
                        Retry
                      </button>
                    )}
                  </div>
                ) : msg.type === "code" ? (
                  <div className="max-w-[90%] sm:max-w-lg">
                    <pre className="my-1 rounded-md bg-zinc-900 px-4 py-3 text-xs leading-relaxed overflow-x-auto">
                      <code>{msg.content}</code>
                    </pre>
                  </div>
                ) : (
                  <div
                    className="max-w-[90%] text-sm leading-relaxed text-muted-foreground sm:max-w-lg"
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(msg.content) }}
                  />
                )}
              </div>
            ))}

            {/* Loading indicator while sending */}
            {isSending && (
              <div className="border-l-2 border-blue-500/40 pl-4 py-2">
                <div className="flex items-center gap-2 text-xs text-blue-400">
                  <Loader2 size={12} className="animate-spin" />
                  <span>Mariana is researching...</span>
                </div>
              </div>
            )}

            {/* Report download buttons */}
            {isCompleted && activeTaskId && (
              <div className="rounded-lg border border-green-500/20 bg-green-500/5 px-4 py-4">
                <div className="flex items-center gap-2 mb-3">
                  <FileText size={16} className="text-green-400" />
                  <span className="text-sm font-medium text-green-400">
                    Investigation Complete
                  </span>
                </div>
                <div className="flex flex-wrap gap-2">
                  <a
                    href={`${API_URL}/api/investigations/${activeTaskId}/report`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 rounded-md bg-green-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-700 transition-colors"
                  >
                    <Download size={12} />
                    Download PDF Report
                  </a>
                  <a
                    href={`${API_URL}/api/investigations/${activeTaskId}/report/docx`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 rounded-md border border-green-600/50 px-3 py-1.5 text-xs font-medium text-green-400 hover:bg-green-600/10 transition-colors"
                  >
                    <Download size={12} />
                    Download Word Report
                  </a>
                </div>
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>
        </div>

        {/* ---------------------------------------------------------- */}
        {/*  Input area                                                */}
        {/* ---------------------------------------------------------- */}
        <div className="border-t border-border px-4 py-4 sm:px-6">
          <div className="mx-auto max-w-2xl">
            {/* Duration selector */}
            <div className="mb-3 flex flex-wrap items-center gap-2 sm:gap-3">
              <div className="relative">
                <select
                  value={duration}
                  onChange={(e) => setDuration(e.target.value)}
                  className="appearance-none rounded-md border border-border bg-card py-1.5 pl-3 pr-8 text-xs text-foreground focus:border-primary focus:outline-none"
                  disabled={isSending}
                >
                  {durationOptions.map((d) => (
                    <option key={d.value} value={d.value}>
                      {d.label}
                      {d.timeLabel ? ` (${d.timeLabel})` : ""}
                    </option>
                  ))}
                </select>
                <ChevronDown
                  size={12}
                  className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"
                />
              </div>

              {/* Custom hours input */}
              {duration === "custom" && (
                <input
                  type="number"
                  min="0.1"
                  step="0.1"
                  value={customHours}
                  onChange={(e) => setCustomHours(e.target.value)}
                  placeholder="Hours"
                  className="w-20 rounded-md border border-border bg-card px-2 py-1.5 text-xs text-foreground placeholder:text-muted-foreground/50 focus:border-primary focus:outline-none"
                  disabled={isSending}
                />
              )}

              {/* Estimated time display */}
              <span className="text-[10px] text-muted-foreground">
                {duration === "custom"
                  ? customHours
                    ? `≈ ${formatDuration(parseFloat(customHours) || 0)}`
                    : "Enter hours"
                  : `≈ ${selectedDuration?.timeLabel}`}
              </span>
            </div>

            {/* High-cost warning */}
            {selectedDuration?.warn && (
              <div className="mb-3 flex items-start gap-2 rounded-md bg-amber-950/50 px-3 py-2 text-xs text-amber-400 ring-1 ring-amber-500/20">
                <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                <span>
                  {duration === "marathon"
                    ? "Marathon research runs for multiple days and consumes significant resources. Ensure your credit balance is sufficient."
                    : duration === "flagship"
                    ? "Flagship research runs for 24–72 hours and can consume a significant number of credits."
                    : "Professional research runs for 6–12 hours. Review the cost estimate before proceeding."}
                </span>
              </div>
            )}

            <form onSubmit={handleSend} className="flex gap-2 sm:gap-3">
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="Describe what you want Mariana to investigate..."
                className="min-w-0 flex-1 rounded-md border border-border bg-card px-3 py-2.5 text-sm text-foreground placeholder:text-muted-foreground/50 focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/20 sm:px-4"
                disabled={isSending}
              />
              <button
                type="submit"
                disabled={isSending || !input.trim()}
                className="flex shrink-0 items-center gap-2 rounded-md bg-primary px-3 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50 sm:px-4"
              >
                <Send size={14} />
              </button>
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}
