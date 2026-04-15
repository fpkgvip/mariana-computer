import { useState, useEffect, useRef, useCallback } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Link, useNavigate } from "react-router-dom";
import {
  Send,
  AlertTriangle,
  Menu,
  X,
  Download,
  FileText,
  RefreshCw,
  Clock,
  Loader2,
  CheckCircle,
  XCircle,
  Paperclip,
  ExternalLink,
  Zap,
  Brain,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { supabase } from "@/lib/supabase";
import ProgressTimeline, {
  parseStructuredEvent,
  type TimelineStep,
  type StructuredEvent,
} from "@/components/ProgressTimeline";
import FileViewer, { FileCard, type FileAttachment } from "@/components/FileViewer";
import FileUpload, { type UploadedFile } from "@/components/FileUpload";

/* ------------------------------------------------------------------ */
/*  Types                                                             */
/* ------------------------------------------------------------------ */

interface Message {
  role: "user" | "assistant" | "system";
  content: string;
  type?: "text" | "code" | "status" | "error" | "plan";
  id?: string; // dedup key for status messages
  _id: string; // stable React key, always set
}

type InvestigationStatus = "PENDING" | "RUNNING" | "COMPLETED" | "FAILED" | "HALTED";

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

/** GET /api/investigations/{task_id} polling response — matches backend TaskSummary */
interface InvestigationPollResponse {
  id: string;                       // BUG-R2-02: backend returns "id" not "task_id"
  status: InvestigationStatus;
  current_state: string;            // BUG-R2-02: backend returns "current_state" not "status_message"
  topic: string;
  total_spent_usd: number;
  output_pdf_path: string | null;
  output_docx_path: string | null;
  error?: string;
}

/** POST /api/investigations/classify response */
interface ClassifyResponse {
  tier: "instant" | "quick" | "standard" | "deep";
  plan_summary: string;
  estimated_duration_hours: number;
  estimated_credits: number;
  requires_approval: boolean;
}

/** Pending research plan to show in the chat before approval */
interface ResearchPlan {
  topic: string;
  tier: ClassifyResponse["tier"];
  plan_summary: string;
  estimated_duration_hours: number;
  estimated_credits: number;
}

/* ------------------------------------------------------------------ */
/*  Constants                                                         */
/* ------------------------------------------------------------------ */

// VITE_API_URL cast is unnecessary — Vite env vars are already string | undefined
const API_URL = import.meta.env.VITE_API_URL ?? "";

/** Generate a stable unique ID for message list keys */
const makeMessageId = () => `msg-${Date.now()}-${Math.random().toString(36).slice(2)}`;

/** Format hours as a human-readable duration string */
const formatDuration = (hours: number): string => {
  if (hours < 1 / 60) return "< 1 min";
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  if (hours === 1) return "1 hour";
  if (hours < 24) return `${hours.toFixed(1).replace(/\.0$/, "")} hours`;
  const days = hours / 24;
  return `${days.toFixed(1).replace(/\.0$/, "")} days`;
};

const STATUS_COLORS: Record<InvestigationStatus, string> = {
  PENDING: "bg-yellow-500/20 text-yellow-400 ring-yellow-500/30",
  RUNNING: "bg-blue-500/20 text-blue-400 ring-blue-500/30",
  COMPLETED: "bg-green-500/20 text-green-400 ring-green-500/30",
  FAILED: "bg-red-500/20 text-red-400 ring-red-500/30",
  HALTED: "bg-red-500/20 text-red-400 ring-red-500/30",
};

/* ------------------------------------------------------------------ */
/*  Helpers                                                           */
/* ------------------------------------------------------------------ */

async function getAccessToken(): Promise<string | null> {
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

/** Authenticated image — fetches via auth header and displays as blob URL */
function AuthImage({ src, alt, className }: { src: string; alt: string; className?: string }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    let url: string | null = null;
    (async () => {
      const token = await getAccessToken();
      if (!token || cancelled) return;
      try {
        const res = await fetch(src, { headers: { Authorization: `Bearer ${token}` } });
        if (!res.ok || cancelled) return;
        const blob = await res.blob();
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setBlobUrl(url);
      } catch { /* silently fail */ }
    })();
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [src]);
  if (!blobUrl) return <div className={`animate-pulse bg-muted rounded ${className ?? ""}`} style={{ minHeight: 80 }} />;
  return <img src={blobUrl} alt={alt} className={className} loading="lazy" />;
}

/** Authenticated video — fetches via auth header and displays as blob URL */
function AuthVideo({ src, ext, className }: { src: string; ext: string; className?: string }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    let url: string | null = null;
    (async () => {
      const token = await getAccessToken();
      if (!token || cancelled) return;
      try {
        const res = await fetch(src, { headers: { Authorization: `Bearer ${token}` } });
        if (!res.ok || cancelled) return;
        const blob = await res.blob();
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setBlobUrl(url);
      } catch { /* silently fail */ }
    })();
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [src]);
  if (!blobUrl) return <div className={`animate-pulse bg-muted rounded ${className ?? ""}`} style={{ minHeight: 80 }} />;
  return (
    <video controls className={className} preload="metadata">
      <source src={blobUrl} type={`video/${ext === "mov" ? "mp4" : ext}`} />
      Your browser does not support video playback.
    </video>
  );
}

/**
 * Safe markdown-ish rendering.
 * Uses bounded quantifiers to prevent ReDoS catastrophic backtracking.
 * Bold is applied before italic so that **bold** is not corrupted by the italic pass.
 * Fenced code blocks use a line-count-bounded approach: split on triple-backtick
 * boundaries rather than a [\s\S]*? lazy dot-all pattern.
 *
 * BUG-R2-14: XSS SAFETY NOTE — Do NOT add link/URL rendering ([text](url)) without
 * first sanitizing the href value against `javascript:` and `data:` URI schemes.
 * Example safe check: if (!/^https?:\/\//i.test(url)) return '...' (strip non-http links).
 * All content is HTML-escaped (& < >) before any markdown substitution, making the
 * current set of transformations safe for dangerouslySetInnerHTML use.
 */
function renderMarkdown(text: string): string {
  // Escape HTML first to prevent XSS from content
  let html = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  // Fenced code blocks: split on ``` boundaries to avoid ReDoS
  // Only process if the string contains at least two ``` markers
  if (html.includes("```")) {
    const parts = html.split("```");
    html = parts
      .map((part, idx) => {
        if (idx % 2 === 0) return part; // outside code block
        // Inside code block: first line may be language hint
        const newlineIdx = part.indexOf("\n");
        const code = newlineIdx !== -1 ? part.slice(newlineIdx + 1) : part;
        return `<pre class="my-2 rounded-md bg-zinc-900 px-4 py-3 text-xs leading-relaxed overflow-x-auto"><code>${code.trim()}</code></pre>`;
      })
      .join("");
  }

  html = html
    // Inline code — [^`]{1,200} bounds the match length to prevent runaway
    .replace(/`([^`]{1,200})`/g, '<code class="rounded bg-zinc-800 px-1.5 py-0.5 text-xs">$1</code>')
    // Bold — [^\n]{1,200} prevents catastrophic backtracking and avoids crossing newlines
    .replace(/\*\*([^\n]{1,200})\*\*/g, "<strong>$1</strong>")
    // Italic — applied after bold so ** is already consumed
    .replace(/\*([^*\n]{1,200})\*/g, "<em>$1</em>")
    // Links — [text](url) with XSS-safe href check (only http/https)
    .replace(/\[([^\]]{1,200})\]\((https?:\/\/[^)]{1,500})\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-0.5 text-primary/80 hover:text-primary text-xs underline decoration-primary/30">$1<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="inline ml-0.5"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg></a>')
    // Newlines
    .replace(/\n/g, "<br />");

  return html;
}

/**
 * Extract all citation URLs from a text string.
 * Returns unique {text, url} pairs from markdown-style links.
 */
function extractCitations(text: string): Array<{ text: string; url: string }> {
  const citations: Array<{ text: string; url: string }> = [];
  const seen = new Set<string>();
  const re = /\[([^\]]{1,200})\]\((https?:\/\/[^)]{1,500})\)/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    const url = match[2];
    if (!seen.has(url)) {
      seen.add(url);
      citations.push({ text: match[1], url });
    }
  }
  return citations;
}

/** Auto-detect skill from topic text (client-side mirror of backend skill detection) */
const SKILL_KEYWORDS: Array<{ id: string; name: string; keywords: string[] }> = [
  { id: "research-report", name: "Research Report", keywords: ["report", "research report", "analysis", "deep dive"] },
  { id: "financial-analysis", name: "Financial Analysis", keywords: ["financial", "earnings", "valuation", "SEC filing", "balance sheet"] },
  { id: "competitive-analysis", name: "Competitive Analysis", keywords: ["competitive", "competition", "market share", "landscape"] },
  { id: "data-analysis", name: "Data Analysis", keywords: ["data", "statistics", "quantitative", "correlation", "regression"] },
  { id: "presentation-builder", name: "Presentation Builder", keywords: ["presentation", "slides", "pptx", "powerpoint", "deck"] },
  { id: "excel-model", name: "Excel Model Builder", keywords: ["excel", "model", "spreadsheet", "dcf", "valuation model"] },
];

function detectSkill(topic: string): { id: string; name: string } | null {
  const lower = topic.toLowerCase();
  for (const skill of SKILL_KEYWORDS) {
    for (const kw of skill.keywords) {
      if (lower.includes(kw.toLowerCase())) {
        return { id: skill.id, name: skill.name };
      }
    }
  }
  return null;
}

/** Format elapsed seconds as "Xh Ym" or "Ym Zs" */
function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) {
    return m > 0 ? `${h}h ${m}m` : `${h}h`;
  }
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

/* ------------------------------------------------------------------ */
/*  Component                                                         */
/* ------------------------------------------------------------------ */

export default function Chat() {
  const { user, refreshUser } = useAuth();
  const navigate = useNavigate();

  // Messages for the currently viewed investigation
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [isClassifying, setIsClassifying] = useState(false);
  const [retryPayload, setRetryPayload] = useState<{ topic: string } | null>(null);

  // Pending research plan awaiting user approval
  const [pendingPlan, setPendingPlan] = useState<ResearchPlan | null>(null);

  // Investigation management
  const [investigations, setInvestigations] = useState<Investigation[]>([]);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);

  // Timer state — elapsed time only
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

  // Timeline steps for structured progress events
  const [timelineSteps, setTimelineSteps] = useState<TimelineStep[]>([]);
  const timelineStoreRef = useRef<Record<string, TimelineStep[]>>({});

  // File viewer state
  const [viewingFile, setViewingFile] = useState<FileAttachment | null>(null);

  // File upload state
  const [uploadedFiles, setUploadedFiles] = useState<UploadedFile[]>([]);
  const [uploadSessionUuid, setUploadSessionUuid] = useState<string | null>(null);

  // Credit animation state
  const [creditAnimating, setCreditAnimating] = useState(false);
  const prevTokensRef = useRef<number>(user?.tokens ?? 0);

  // Memory panel state
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [memoryFacts, setMemoryFacts] = useState<Array<{ fact: string; category: string }>>([]);
  const [memoryPrefs, setMemoryPrefs] = useState<Record<string, string>>({});
  const [memoryLoading, setMemoryLoading] = useState(false);

  // Stable ref to current messages — prevents stale closure in switchInvestigation.
  // BUG-R2-11: Assign directly during render instead of via useEffect.
  // React docs explicitly sanction writing to a ref during render for this synchronization
  // pattern, and it avoids an unnecessary effect + potential lint warnings.
  const messagesRef = useRef<Message[]>([]);
  messagesRef.current = messages;

  /* ---------------------------------------------------------------- */
  /*  Auth guard                                                      */
  /* ---------------------------------------------------------------- */

  // BUG-009: Add a brief grace period before redirecting so we don't
  // false-logout during a Supabase token refresh (which briefly sets user=null).
  useEffect(() => {
    if (!user) {
      const timer = setTimeout(() => navigate("/login"), 500);
      return () => clearTimeout(timer);
    }
  }, [user, navigate]);

  /* ---------------------------------------------------------------- */
  /*  Credit change animation                                         */
  /* ---------------------------------------------------------------- */

  // BUG-R2-S2-07: prevTokensRef was only updated when credits did NOT change
  // (the early return skipped the assignment). This caused the animation to
  // re-trigger on every re-render after the first credit change.
  useEffect(() => {
    if (!user) return;
    if (prevTokensRef.current !== user.tokens && prevTokensRef.current > 0) {
      setCreditAnimating(true);
      const timer = setTimeout(() => setCreditAnimating(false), 1500);
      prevTokensRef.current = user.tokens;
      return () => clearTimeout(timer);
    }
    prevTokensRef.current = user.tokens;
  }, [user?.tokens]);

  /* ---------------------------------------------------------------- */
  /*  Memory panel helpers                                            */
  /* ---------------------------------------------------------------- */

  const loadMemory = useCallback(async () => {
    setMemoryLoading(true);
    try {
      const token = await getAccessToken();
      if (!token) return;
      const res = await fetch(`${API_URL}/api/memory`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        const data = await res.json();
        setMemoryFacts(data.facts || []);
        setMemoryPrefs(data.preferences || {});
      }
    } catch {
      // Memory API may not exist yet — silently ignore
    } finally {
      setMemoryLoading(false);
    }
  }, []);

  const deleteMemoryFact = useCallback(async (fact: string) => {
    try {
      const token = await getAccessToken();
      if (!token) return;
      await fetch(`${API_URL}/api/memory/facts`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ fact }),
      });
      setMemoryFacts((prev) => prev.filter((f) => f.fact !== fact));
      toast.success("Memory entry deleted");
    } catch {
      toast.error("Failed to delete memory entry");
    }
  }, []);

  const deleteMemoryPref = useCallback(async (key: string) => {
    try {
      const token = await getAccessToken();
      if (!token) return;
      await fetch(`${API_URL}/api/memory/preferences`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      setMemoryPrefs((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
      toast.success("Preference deleted");
    } catch {
      toast.error("Failed to delete preference");
    }
  }, []);

  /* ---------------------------------------------------------------- */
  /*  Load investigations from Supabase on mount                      */
  /* ---------------------------------------------------------------- */

  useEffect(() => {
    if (!user) return;
    const loadInvestigations = async () => {
      // BUG-013: Always filter by user_id as defense-in-depth (don't rely solely on RLS)
      const { data, error } = await supabase
        .from("investigations")
        .select("task_id, topic, status, created_at, duration_hours, budget_usd")
        .eq("user_id", user.id)
        .order("created_at", { ascending: false });
      if (error) {
        console.error("[Chat] Failed to load investigations:", error.message);
        toast.error("Failed to load investigations", { description: error.message });
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
    if (msg.type === "status") {
      seenStatusIds.current.add(msgId);
      // BUG-019 / BUG-R1-25: Cap seenStatusIds to prevent unbounded memory growth.
      // Use a sliding-window trim instead of clearing entirely — a full clear
      // would remove dedup protection for the next 1000 messages, potentially
      // causing visible duplicates in long-running marathon investigations.
      if (seenStatusIds.current.size > 1000) {
        const entries = [...seenStatusIds.current];
        seenStatusIds.current = new Set(entries.slice(-500));
      }
    }
    // Ensure every message has a stable _id for React keying
    const msgWithId: Message = msg._id ? msg : { ...msg, _id: makeMessageId() };
    setMessages((prev) => [...prev, msgWithId]);
  }, []);

  /* ---------------------------------------------------------------- */
  /*  Process structured SSE event into timeline step                 */
  /* ---------------------------------------------------------------- */

  const processStructuredEvent = useCallback((event: StructuredEvent) => {
    if (event.type === "step_complete" || event.type === "step_error") {
      // Update an existing step in-place
      setTimelineSteps((prev) => {
        const idx = prev.findIndex((s) => s.id === event.step_id);
        if (idx >= 0) {
          const updated = [...prev];
          const existing = updated[idx];
          updated[idx] = {
            ...existing,
            type: event.type as TimelineStep["type"],
            status: event.type === "step_error" ? "error" : "complete",
            duration_ms: event.duration_ms ?? existing.duration_ms,
            detail: event.message ?? existing.detail,
          };
          return updated;
        }
        // Step not found — create a new one
        const newStep = parseStructuredEvent(event, prev);
        return newStep ? [...prev, newStep] : prev;
      });
    } else {
      setTimelineSteps((prev) => {
        const newStep = parseStructuredEvent(event, prev);
        return newStep ? [...prev, newStep] : prev;
      });
    }

    // For file_attached events, also add a message so it shows in the chat
    if (event.type === "file_attached" && event.filename) {
      appendMessage({
        role: "system",
        content: JSON.stringify({
          type: "file_attached",
          filename: event.filename,
          size: event.size ?? 0,
          mime: event.mime,
        }),
        type: "status",
        id: `file-${event.filename}`,
        _id: makeMessageId(),
      });
    }

    // For cost_update events at investigation completion, show credit summary
    if (event.type === "cost_update" && event.spent_usd != null) {
      const creditsUsed = Math.round(event.spent_usd * 1.20 * 100);
      appendMessage({
        role: "system",
        content: JSON.stringify({
          type: "cost_summary",
          credits_used: creditsUsed,
          spent_usd: event.spent_usd,
          budget_usd: event.budget_usd,
        }),
        type: "status",
        id: `cost-final-${Date.now()}`,
        _id: makeMessageId(),
      });
    }
  }, [appendMessage]);

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

  /**
   * BUG-003: Separate "stop connections" from "set sending=false".
   * stopConnectionsOnly is used inside handleSend to avoid a race where
   * isSending is set to false then immediately back to true.
   */
  const stopConnectionsOnly = useCallback(() => {
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    stopTimer();
  }, [stopTimer]);

  const stopAllConnections = useCallback(() => {
    stopConnectionsOnly();
    setIsSending(false);
  }, [stopConnectionsOnly]);

  /* ---------------------------------------------------------------- */
  /*  Polling fallback                                                */
  /* ---------------------------------------------------------------- */

  const startPolling = useCallback(
    (taskId: string, _initialToken: string) => {
      const poll = async () => {
        try {
          // BUG-R1-02: Get a fresh token on every poll tick instead of reusing
          // the captured string. The initial token expires after ~1 hour;
          // for Flagship/Marathon investigations (24h–5 days) this caused 401s
          // that force-logged users out mid-investigation.
          const freshToken = await getAccessToken();
          if (!freshToken) {
            stopAllConnections();
            toast.error("Session expired", { description: "Please sign in again." });
            navigate("/login");
            return;
          }
          const res = await fetch(`${API_URL}/api/investigations/${taskId}`, {
            headers: {
              Authorization: `Bearer ${freshToken}`,
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
              _id: makeMessageId(),
            });
            return;
          }

          if (!res.ok) return;

          // BUG-R2-02: Backend returns TaskSummary — use "id", "current_state" not "task_id", "status_message"
          const data: InvestigationPollResponse = await res.json();

          // Show current_state as a progress message (backend state-machine string)
          if (data.current_state) {
            appendMessage({
              role: "system",
              content: data.current_state,
              type: "status",
              id: `poll-state-${data.current_state}`,
              _id: makeMessageId(),
            });
          }

          if (data.status === "RUNNING") {
            updateInvestigationStatus(taskId, "RUNNING");
          }

          if (data.status === "COMPLETED") {
            updateInvestigationStatus(taskId, "COMPLETED");
            // BUG-R2-02: Backend has no "findings" field in TaskSummary.
            // Report content is available only via the download endpoints.
            // Show a completion message and let the download buttons handle retrieval.
            appendMessage({
              role: "system",
              content: "Investigation complete. Reports are ready for download.",
              type: "status",
              id: `completed-${taskId}`,
              _id: makeMessageId(),
            });
            stopAllConnections();
            // BUG-018: Refresh credit balance after investigation completes
            refreshUser();
          } else if (data.status === "FAILED" || data.status === "HALTED") {
            updateInvestigationStatus(taskId, "FAILED");
            const errMsg = data.error ?? "The investigation failed. Please try again.";
            appendMessage({
              role: "assistant",
              content: errMsg,
              type: "text",
              _id: makeMessageId(),
            });
            toast.error("Investigation failed", { description: errMsg });
            stopAllConnections();
            // BUG-018: Refresh credit balance after investigation completes
            refreshUser();
          }
        } catch (err) {
          console.error("[Chat] Polling error:", err);
        }
      };

      poll();
      pollIntervalRef.current = setInterval(poll, 5000);
    },
    [appendMessage, navigate, refreshUser, stopAllConnections, updateInvestigationStatus]
  );

  /* ---------------------------------------------------------------- */
  /*  SSE streaming                                                   */
  /* ---------------------------------------------------------------- */

  const startSSE = useCallback(
    (taskId: string, token: string) => {
      // BUG-001 / BUG-R1-09: Native EventSource cannot send custom headers,
      // so the auth token must be passed as a URL query parameter instead.
      // KNOWN TRADE-OFF: The token will appear in server access logs, browser
      // history, and any monitoring tools that record full URLs.
      // Mitigations:
      //   1. Configure the backend to redact the `token` query param from logs.
      //   2. Ideally, use a short-lived "stream token" from a dedicated endpoint
      //      (POST /api/investigations/{id}/stream-token) rather than the
      //      long-lived JWT, so a leaked URL has a narrow exposure window.
      //   3. Long-term: replace EventSource with fetch + ReadableStream to
      //      allow proper Authorization headers.
      const url = `${API_URL}/api/investigations/${taskId}/logs?token=${encodeURIComponent(token)}`;

      try {
        const es = new EventSource(url);
        eventSourceRef.current = es;

        // BUG-002: Guard against multiple fallback polling loops
        let hasFailedOver = false;

        es.onmessage = (event) => {
          try {
            const parsed = JSON.parse(event.data);

            // Check if this is a structured progress event (has a "type" field)
            const eventType = parsed.type as string | undefined;
            if (eventType && [
              "step_start", "step_complete", "step_error", "status_change",
              "file_attached", "cost_update", "hypothesis_update", "text",
            ].includes(eventType)) {
              processStructuredEvent(parsed as StructuredEvent);

              // Also check for terminal status in structured events
              if (eventType === "status_change" && parsed.state === "HALT") {
                updateInvestigationStatus(taskId, "COMPLETED");
                appendMessage({
                  role: "system",
                  content: "Investigation complete. Reports are ready for download.",
                  type: "status",
                  id: `completed-${taskId}`,
                  _id: makeMessageId(),
                });
                stopAllConnections();
                refreshUser();
              }
              return;
            }

            // Legacy event format — fallback
            const content = parsed.message || parsed.content || parsed.data || event.data;
            const status = parsed.status as InvestigationStatus | undefined;

            appendMessage({
              role: "system",
              content: String(content),
              type: "status",
              id: `sse-${event.lastEventId || content}`,
              _id: makeMessageId(),
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
                  _id: makeMessageId(),
                });
              }
              appendMessage({
                role: "system",
                content: "Investigation complete. Reports are ready for download.",
                type: "status",
                id: `completed-${taskId}`,
                _id: makeMessageId(),
              });
              stopAllConnections();
              refreshUser();
            } else if (status === "FAILED") {
              updateInvestigationStatus(taskId, "FAILED");
              appendMessage({
                role: "assistant",
                content: parsed.error || "The investigation failed.",
                type: "text",
                _id: makeMessageId(),
              });
              stopAllConnections();
              refreshUser();
            }
          } catch {
            // Plain text SSE event — add as legacy status message
            appendMessage({
              role: "system",
              content: event.data,
              type: "status",
              id: `sse-${event.data}`,
              _id: makeMessageId(),
            });
          }
        };

        // BUG-R2-08: Register named event listeners for server-emitted event types.
        // es.onmessage only handles the default (unnamed) event type.
        // The backend emits: "done", "ping", "state_change", "error" — all ignored by onmessage.

        es.addEventListener("done", (event: MessageEvent) => {
          try {
            const parsed = JSON.parse(event.data);
            const finalStatus = (parsed.final_status || parsed.status) as InvestigationStatus;
            if (finalStatus === "COMPLETED") {
              updateInvestigationStatus(taskId, "COMPLETED");
              appendMessage({
                role: "system",
                content: "Investigation complete. Reports are ready for download.",
                type: "status",
                id: `completed-${taskId}`,
                _id: makeMessageId(),
              });
              stopAllConnections();
              refreshUser();
            } else if (finalStatus === "FAILED" || finalStatus === "HALTED") {
              updateInvestigationStatus(taskId, "FAILED");
              appendMessage({
                role: "assistant",
                content: parsed.error || `Investigation ${finalStatus}.`,
                type: "text",
                _id: makeMessageId(),
              });
              stopAllConnections();
              refreshUser();
            }
          } catch {
            // Non-JSON done event — treat as completion signal
            updateInvestigationStatus(taskId, "COMPLETED");
            appendMessage({
              role: "system",
              content: "Investigation complete. Reports are ready for download.",
              type: "status",
              id: `completed-${taskId}`,
              _id: makeMessageId(),
            });
            stopAllConnections();
            refreshUser();
          }
        });

        es.addEventListener("state_change", (event: MessageEvent) => {
          try {
            const parsed = JSON.parse(event.data);
            if (parsed.status) updateInvestigationStatus(taskId, parsed.status as InvestigationStatus);
            const stateMsg = parsed.state || parsed.current_state || parsed.message;
            if (stateMsg) {
              appendMessage({
                role: "system",
                content: String(stateMsg),
                type: "status",
                id: `state-change-${stateMsg}`,
                _id: makeMessageId(),
              });
            }
          } catch {}
        });

        // Named "error" event from the server (distinct from connection errors via es.onerror)
        es.addEventListener("error", (event: MessageEvent) => {
          try {
            const parsed = JSON.parse(event.data);
            const errMsg = parsed.error || parsed.message || "Stream error";
            appendMessage({
              role: "assistant",
              content: errMsg,
              type: "error",
              _id: makeMessageId(),
            });
          } catch {}
        });

        // BUG-002: hasFailedOver flag prevents multiple concurrent polling loops
        es.onerror = async () => {
          if (hasFailedOver) return;
          hasFailedOver = true;
          console.warn("[Chat] SSE connection error, falling back to polling.");
          es.close();
          eventSourceRef.current = null;
          // BUG-R1-02: Fetch fresh token for polling fallover — the SSE token
          // may have been created some time ago and could be near expiry.
          const freshToken = await getAccessToken();
          // BUG-R2-17: Guard against starting a polling loop if the component
          // unmounted during the async getAccessToken() call.
          if (freshToken && pollIntervalRef.current === null) {
            startPolling(taskId, freshToken);
          }
        };
      } catch {
        console.warn("[Chat] SSE not available, using polling.");
        // Pass token through for initial call; poll() will refresh on each tick
        startPolling(taskId, token);
      }
    },
    [appendMessage, processStructuredEvent, refreshUser, startPolling, stopAllConnections, updateInvestigationStatus]
  );

  /* ---------------------------------------------------------------- */
  /*  Switch active investigation                                     */
  /* ---------------------------------------------------------------- */

  const switchInvestigation = useCallback(
    (taskId: string) => {
      // BUG-004: Use messagesRef.current instead of messages from closure
      // to avoid stale snapshot and unnecessary re-creation on every message append
      if (activeTaskId && messagesRef.current.length > 0) {
        messageStoreRef.current[activeTaskId] = [...messagesRef.current];
      }
      // Save timeline steps for current investigation
      if (activeTaskId) {
        timelineStoreRef.current[activeTaskId] = [...timelineSteps];
      }

      // Stop any active connections
      stopAllConnections();

      // Dismiss any pending plan when switching investigations
      setPendingPlan(null);

      // Clear file upload state when switching
      setUploadedFiles([]);
      setUploadSessionUuid(null);

      // Load stored messages for the target investigation
      // BUG-R2-06: Fall back to messages currently shown if no store entry
      // (e.g. investigation loaded from Supabase on mount but never viewed this session)
      const stored = messageStoreRef.current[taskId];
      const targetMessages = stored || [];
      setMessages(targetMessages);
      setActiveTaskId(taskId);
      setSidebarOpen(false);

      // Restore timeline steps for target investigation
      setTimelineSteps(timelineStoreRef.current[taskId] || []);

      // BUG-R2-06: Reseed seenStatusIds from the messages we're about to display.
      // Must happen after clear() so we don't leave stale IDs from the previous investigation.
      seenStatusIds.current.clear();
      targetMessages.forEach((m) => {
        if (m.id) seenStatusIds.current.add(m.id);
      });

      // If investigation is still running, reconnect SSE
      const inv = investigations.find((i) => i.task_id === taskId);
      if (inv && (inv.status === "RUNNING" || inv.status === "PENDING")) {
        setIsSending(true);
        getAccessToken().then((token) => {
          if (token) {
            startTimer();
            startSSE(taskId, token);
          }
        });
      }
    },
    // messages removed from deps — using messagesRef.current instead
    [activeTaskId, investigations, timelineSteps, stopAllConnections, startSSE, startTimer]
  );

  /* ---------------------------------------------------------------- */
  /*  Classify topic and start investigation flow                     */
  /* ---------------------------------------------------------------- */

  /**
   * Entry point: user hits send. We classify the topic first.
   * - instant tier → skip plan, go straight to startInvestigation
   * - standard/deep tier → show ResearchPlan card for user approval
   */
  const handleSend = useCallback(async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (isSending || isClassifying) return;

    const topic = input.trim() || retryPayload?.topic || "";
    if (!topic) return;

    setRetryPayload(null);
    setInput("");

    // Save current investigation messages before starting new
    if (activeTaskId && messagesRef.current.length > 0) {
      messageStoreRef.current[activeTaskId] = [...messagesRef.current];
    }

    // BUG-003: Use stopConnectionsOnly to avoid race condition where
    // stopAllConnections sets isSending=false then we immediately set it true
    seenStatusIds.current.clear();
    stopConnectionsOnly();
    setPendingPlan(null);

    // Clear timeline for new investigation
    setTimelineSteps([]);

    const newMessages: Message[] = [
      { role: "user", content: topic, type: "text", _id: makeMessageId() },
    ];
    setMessages(newMessages);

    // Get auth token
    const token = await getAccessToken();
    if (!token) {
      toast.error("Not authenticated", {
        description: "Please sign in to run an investigation.",
      });
      navigate("/login");
      return;
    }

    setIsClassifying(true);

    try {
      const classifyRes = await fetch(`${API_URL}/api/investigations/classify`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ topic }),
      });

      if (classifyRes.status === 401) {
        toast.error("Session expired", { description: "Please sign in again." });
        navigate("/login");
        return;
      }

      if (!classifyRes.ok) {
        // Classify failed — fall through to direct investigation start
        console.warn("[Chat] Classify failed, starting investigation directly.");
        setIsClassifying(false);
        await startInvestigation(topic, token, true);
        return;
      }

      const classifyData: ClassifyResponse = await classifyRes.json();
      setIsClassifying(false);

      if (classifyData.tier === "instant" || classifyData.tier === "quick") {
        // No approval needed for instant/quick — go straight to investigation
        await startInvestigation(topic, token, true);
      } else {
        // Show research plan card for user approval
        setPendingPlan({
          topic,
          tier: classifyData.tier,
          plan_summary: classifyData.plan_summary,
          estimated_duration_hours: classifyData.estimated_duration_hours,
          estimated_credits: classifyData.estimated_credits,
        });
      }
    } catch (err) {
      setIsClassifying(false);
      console.warn("[Chat] Classify error, starting investigation directly:", err);
      await startInvestigation(topic, token, true);
    }
  // BUG-R2-01: Dependency array for useCallback — all captured values listed.
  // Note: startInvestigation is defined AFTER handleSend, so it cannot be included
  // in the deps array (temporal dead zone). This is acceptable because the closure
  // captures the variable by reference — when handleSend is *called*, startInvestigation
  // will have been assigned in the same render cycle. The minor staleness risk is
  // mitigated by handleSend re-creating whenever its own deps change.
  }, [isSending, isClassifying, input, retryPayload, activeTaskId, stopConnectionsOnly, navigate]);

  /* ---------------------------------------------------------------- */
  /*  Start investigation (after classify or after plan approval)     */
  /* ---------------------------------------------------------------- */

  const startInvestigation = useCallback(async (
    topic: string,
    token: string,
    planApproved: boolean,
  ) => {
    setIsSending(true);
    setPendingPlan(null);

    // Show initializing status
    const initMsg: Message = {
      role: "system",
      content: "Initializing research environment...",
      type: "status",
      id: "init",
      _id: makeMessageId(),
    };
    seenStatusIds.current.add("init");
    setMessages((prev) => [...prev, initMsg]);

    try {
      const requestBody: Record<string, unknown> = {
        topic,
        plan_approved: planApproved,
      };
      if (uploadSessionUuid) {
        requestBody.upload_session_uuid = uploadSessionUuid;
      }

      const res = await fetch(`${API_URL}/api/investigations`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(requestBody),
      });

      if (res.status === 401) {
        setIsSending(false);
        toast.error("Session expired", { description: "Please sign in again." });
        navigate("/login");
        return;
      }

      if (res.status === 402) {
        setIsSending(false);
        setRetryPayload({ topic });
        const errorData = await res.json().catch(() => ({}));
        const detail = errorData.detail || "Insufficient credits to start this investigation.";
        const estimated = errorData.estimated_credits;
        const balance = user?.tokens ?? 0;
        const msg = estimated
          ? `Insufficient credits to start this investigation. Estimated cost: ${Number(estimated).toLocaleString()} credits. Your balance: ${balance.toLocaleString()} credits.`
          : detail;
        toast.error("Insufficient credits", { description: msg });
        appendMessage({
          role: "system",
          content: msg,
          type: "error",
          id: `credits-${Date.now()}`,
          _id: makeMessageId(),
        });
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
          _id: makeMessageId(),
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
        duration_hours: 0,
        budget_usd: 0,
      };

      setInvestigations((prev) => [newInvestigation, ...prev]);
      setActiveTaskId(taskId);

      // BUG-005: Guard against null user before Supabase insert
      if (!user?.id) {
        console.error("[Chat] Cannot persist investigation: no user ID");
      } else {
        supabase
          .from("investigations")
          .insert({
            task_id: taskId,
            topic,
            status: "PENDING",
            duration_hours: 0,
            budget_usd: 0,
            user_id: user.id, // guaranteed non-null
          })
          .then(({ error }) => {
            if (error) console.error("[Chat] Failed to persist investigation:", error.message);
          });
      }

      appendMessage({
        role: "system",
        content: `Investigation started (ID: ${taskId}). Monitoring progress...`,
        type: "status",
        id: `started-${taskId}`,
        _id: makeMessageId(),
      });

      // Clear upload state after investigation starts
      setUploadedFiles([]);
      setUploadSessionUuid(null);

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
        _id: makeMessageId(),
      });
      setIsSending(false);
    }
  }, [user, uploadSessionUuid, appendMessage, startTimer, startSSE, navigate]);

  /* ---------------------------------------------------------------- */
  /*  Approve research plan                                           */
  /* ---------------------------------------------------------------- */

  const handleApprovePlan = useCallback(async () => {
    if (!pendingPlan) return;
    const { topic } = pendingPlan;
    setPendingPlan(null);

    const token = await getAccessToken();
    if (!token) {
      toast.error("Not authenticated", { description: "Please sign in again." });
      navigate("/login");
      return;
    }

    await startInvestigation(topic, token, true);
  }, [pendingPlan, startInvestigation, navigate]);

  /* ---------------------------------------------------------------- */
  /*  Cancel research plan                                            */
  /* ---------------------------------------------------------------- */

  const handleCancelPlan = useCallback(() => {
    setPendingPlan(null);
    // Remove the user message and plan from the chat — reset to empty
    setMessages([]);
    setTimelineSteps([]);
    setActiveTaskId(null);
    setUploadedFiles([]);
    setUploadSessionUuid(null);
    seenStatusIds.current.clear();
  }, []);

  /* ---------------------------------------------------------------- */
  /*  Retry handler                                                   */
  /* ---------------------------------------------------------------- */

  // BUG-R2-01: Wrapped in useCallback so handleSend reference is always fresh
  const handleRetry = useCallback(() => {
    if (retryPayload) handleSend();
  }, [retryPayload, handleSend]);

  /* ---------------------------------------------------------------- */
  /*  Report download (BUG-007: use fetch with auth header)           */
  /* ---------------------------------------------------------------- */

  const handleDownload = useCallback(async (format: "pdf" | "docx") => {
    if (!activeTaskId) return;
    const token = await getAccessToken();
    if (!token) {
      toast.error("Not authenticated", { description: "Please sign in again." });
      return;
    }
    const endpoint =
      format === "pdf"
        ? `/api/investigations/${activeTaskId}/report`
        : `/api/investigations/${activeTaskId}/report/docx`;
    try {
      const res = await fetch(`${API_URL}${endpoint}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        toast.error("Download failed", { description: `Server returned ${res.status}` });
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `investigation-${activeTaskId}.${format}`;
      // BUG-R2-15: Append/remove anchor from DOM for maximum browser compatibility.
      // Edge historically required the element to be in the DOM for .click() to trigger a download.
      // BUG-R1-19: Revoke asynchronously — browser download initiation is
      // async, so synchronous revocation can cause empty/failed downloads in
      // Firefox and some other browsers.
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 100);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      toast.error("Download failed", { description: msg });
    }
  }, [activeTaskId]);

  /* ---------------------------------------------------------------- */
  /*  Derived state                                                   */
  /* ---------------------------------------------------------------- */

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
            aria-label="Close sidebar"
          >
            <X size={18} />
          </button>
        </div>

        {/* New investigation button */}
        <div className="px-4 pt-4">
          <button
            onClick={() => {
              if (activeTaskId && messagesRef.current.length > 0) {
                messageStoreRef.current[activeTaskId] = [...messagesRef.current];
              }
              if (activeTaskId) {
                timelineStoreRef.current[activeTaskId] = [...timelineSteps];
              }
              stopAllConnections();
              setActiveTaskId(null);
              setMessages([]);
              setTimelineSteps([]);
              setPendingPlan(null);
              setUploadedFiles([]);
              setUploadSessionUuid(null);
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
            {investigations.map((inv) => {
              const isActive = activeTaskId === inv.task_id;
              const isInvRunning = inv.status === "RUNNING" || inv.status === "PENDING";
              return (
                <button
                  key={inv.task_id}
                  onClick={() => switchInvestigation(inv.task_id)}
                  className={`w-full rounded-md px-3 py-2 text-left text-xs transition-colors ${
                    isActive
                      ? "bg-secondary text-foreground ring-1 ring-primary/30"
                      : "text-muted-foreground hover:bg-secondary/50 hover:text-foreground"
                  } ${isInvRunning && !isActive ? "ring-1 ring-blue-500/20" : ""}`}
                >
                  <div className="flex items-center gap-2">
                    {isInvRunning && (
                      <span className="relative flex h-2 w-2 shrink-0">
                        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-400 opacity-75" />
                        <span className="relative inline-flex h-2 w-2 rounded-full bg-blue-500" />
                      </span>
                    )}
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
              );
            })}
          </div>
        </div>

        {/* User info */}
        <div className="border-t border-border px-4 py-3">
          <div className="flex items-center justify-between">
            <div className="text-xs text-muted-foreground">
              <span className={`font-medium text-foreground ${creditAnimating ? "animate-credit-pulse" : ""}`}>
                {user.tokens.toLocaleString()}
              </span>{" "}
              credits
            </div>
            <button
              onClick={() => { setMemoryOpen(true); loadMemory(); }}
              className="rounded-md p-1.5 text-muted-foreground/50 hover:text-primary hover:bg-secondary/50 transition-colors"
              title="Memory"
            >
              <Brain size={14} />
            </button>
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

          {/* Running indicator + elapsed timer */}
          <div className="flex items-center gap-3">
            {isRunning && isSending && (
              <div className="flex items-center gap-2 text-xs text-blue-400">
                <span className="relative flex h-2 w-2">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-400 opacity-75" />
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-blue-500" />
                </span>
                <Clock size={12} />
                <span className="font-mono">Elapsed: {formatElapsed(elapsedSeconds)}</span>
              </div>
            )}
            <div className={`text-xs text-muted-foreground md:hidden ${creditAnimating ? "animate-credit-pulse" : ""}`}>
              {user.tokens.toLocaleString()} credits
            </div>
          </div>
        </div>

        {/* ---------------------------------------------------------- */}
        {/*  Messages area                                             */}
        {/* ---------------------------------------------------------- */}
        <div
          ref={messagesContainerRef}
          className="flex-1 overflow-y-auto px-4 py-6 sm:px-6"
        >
          <div className="mx-auto max-w-2xl space-y-4">
            {/* Zero credits banner */}
            {user.tokens <= 0 && (
              <div className="rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-3">
                <div className="flex items-center gap-2">
                  <AlertTriangle size={14} className="shrink-0 text-red-400" />
                  <span className="text-sm font-medium text-red-400">
                    You&apos;re out of credits. Upgrade your plan to continue researching.
                  </span>
                </div>
                <Link
                  to="/pricing"
                  className="mt-2 inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
                >
                  Upgrade
                </Link>
              </div>
            )}

            {/* Low credits warning */}
            {user.tokens > 0 && user.tokens < 1000 && (
              <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2">
                <div className="flex items-center gap-2 text-xs text-amber-400">
                  <AlertTriangle size={12} className="shrink-0" />
                  <span>Low credits: {user.tokens.toLocaleString()} remaining</span>
                  <Link
                    to="/pricing"
                    className="ml-auto text-[10px] underline underline-offset-2 hover:text-amber-300"
                  >
                    Upgrade
                  </Link>
                </div>
              </div>
            )}

            {/* Empty state */}
            {messages.length === 0 && !isSending && !pendingPlan && (
              <div className="flex flex-col items-center justify-center py-24 text-center">
                <h2 className="font-serif text-xl font-semibold text-foreground mb-2">
                  What would you like to know?
                </h2>
                <p className="text-sm text-muted-foreground max-w-md">
                  Ask anything. Mariana adapts — from quick answers to multi-day investigations.
                </p>
              </div>
            )}

            {/* Messages */}
            {messages.filter(Boolean).map((msg, i) => (
              <div
                key={msg._id || `fallback-${i}`}
                className="animate-fade-in"
                style={{ animationDelay: `${Math.min(i * 30, 300)}ms` }}
              >
                {msg.role === "user" ? (
                  <div className="flex justify-end">
                    <div className="max-w-[85%] rounded-lg bg-primary/5 px-4 py-3 text-sm leading-relaxed text-foreground ring-1 ring-primary/10 sm:max-w-md">
                      {msg.content}
                    </div>
                  </div>
                ) : msg.type === "status" && (() => {
                  // Try to parse structured content for special rendering
                  try {
                    const parsed = JSON.parse(msg.content);
                    if (parsed.type === "file_attached") return true;
                    if (parsed.type === "cost_summary") return true;
                  } catch { /* not JSON, render normally */ }
                  return false;
                })() ? (
                  (() => {
                    try {
                      const parsed = JSON.parse(msg.content);
                      if (parsed.type === "file_attached") {
                        const ext = (parsed.filename as string).split(".").pop()?.toLowerCase() || "";
                        const isImage = ["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext);
                        const isVideo = ["mp4", "webm", "mov"].includes(ext);
                        const fileUrl = `${API_URL}/api/investigations/${activeTaskId || ""}/files/${encodeURIComponent(parsed.filename)}`;

                        if (isImage) {
                          return (
                            <div className="space-y-1">
                              <button
                                onClick={() =>
                                  setViewingFile({
                                    filename: parsed.filename,
                                    size: parsed.size,
                                    mime: parsed.mime,
                                    taskId: activeTaskId || "",
                                  })
                                }
                                className="block max-w-sm rounded-md overflow-hidden border border-border hover:ring-1 hover:ring-primary/30 transition-all cursor-pointer"
                              >
                                <AuthImage
                                  src={fileUrl}
                                  alt={parsed.filename}
                                  className="max-h-64 w-auto object-contain bg-black/20"
                                />
                              </button>
                              <p className="text-[10px] text-muted-foreground/50">{parsed.filename}</p>
                            </div>
                          );
                        }

                        if (isVideo) {
                          return (
                            <div className="space-y-1 max-w-md">
                              <AuthVideo
                                src={fileUrl}
                                ext={ext}
                                className="w-full rounded-md border border-border"
                              />
                              <p className="text-[10px] text-muted-foreground/50">{parsed.filename}</p>
                            </div>
                          );
                        }

                        return (
                          <FileCard
                            filename={parsed.filename}
                            size={parsed.size}
                            onClick={() =>
                              setViewingFile({
                                filename: parsed.filename,
                                size: parsed.size,
                                mime: parsed.mime,
                                taskId: activeTaskId || "",
                              })
                            }
                          />
                        );
                      }
                      if (parsed.type === "cost_summary") {
                        return (
                          <div className="rounded-md border border-border bg-card/50 px-3 py-2 text-xs text-muted-foreground">
                            <span className="font-medium text-foreground">
                              {Number(parsed.credits_used).toLocaleString()} credits used
                            </span>
                            {" "}
                            (${Number(parsed.spent_usd).toFixed(2)} + 20% markup)
                          </div>
                        );
                      }
                    } catch { /* fallthrough */ }
                    return null;
                  })()
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
                  <div className="max-w-[90%] sm:max-w-lg">
                    <div
                      className="text-sm leading-relaxed text-muted-foreground"
                      dangerouslySetInnerHTML={{ __html: renderMarkdown(msg.content) }}
                    />
                    {(() => {
                      const citations = extractCitations(msg.content);
                      if (citations.length === 0) return null;
                      return (
                        <div className="mt-3 border-t border-border/50 pt-2">
                          <p className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/50 mb-1">
                            Sources
                          </p>
                          <div className="flex flex-wrap gap-x-3 gap-y-1">
                            {citations.map((c) => (
                              <a
                                key={c.url}
                                href={c.url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center gap-0.5 text-[11px] text-primary/70 hover:text-primary underline decoration-primary/20 underline-offset-2"
                              >
                                {c.text}
                                <ExternalLink size={9} />
                              </a>
                            ))}
                          </div>
                        </div>
                      );
                    })()}
                  </div>
                )}
              </div>
            ))}

            {/* Progress Timeline — structured step events */}
            {timelineSteps.length > 0 && (
              <div className="border-l-2 border-blue-500/20 pl-4 py-2">
                <ProgressTimeline
                  steps={timelineSteps}
                  onFileClick={(filename) => {
                    if (activeTaskId) {
                      setViewingFile({
                        filename,
                        size: 0,
                        taskId: activeTaskId,
                      });
                    }
                  }}
                />
              </div>
            )}

            {/* Research Plan Card — shown after classify for standard/deep tiers */}
            {pendingPlan && (
              <div className="animate-fade-in rounded-lg border border-border bg-card p-5 shadow-sm">
                <p className="mb-1 text-[10px] font-medium uppercase tracking-[0.15em] text-muted-foreground">
                  Research Plan
                </p>
                <p className="mt-3 text-sm leading-relaxed text-foreground">
                  {pendingPlan.plan_summary}
                </p>
                <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                  <span className="flex items-center gap-1">
                    <Clock size={11} />
                    Estimated: ~{formatDuration(pendingPlan.estimated_duration_hours)}
                  </span>
                  <span>·</span>
                  <span>{pendingPlan.estimated_credits.toLocaleString()} credits</span>
                </div>
                <div className="mt-4 flex flex-wrap gap-2">
                  <button
                    onClick={handleApprovePlan}
                    className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
                  >
                    <CheckCircle size={13} />
                    Approve &amp; Start
                  </button>
                  <button
                    onClick={handleCancelPlan}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border px-4 py-2 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
                  >
                    <XCircle size={13} />
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {/* Loading indicator while classifying */}
            {isClassifying && (
              <div className="border-l-2 border-accent/40 pl-4 py-2">
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 size={12} className="animate-spin" />
                  <span>Analyzing your question...</span>
                </div>
              </div>
            )}

            {/* Loading indicator while investigation is running */}
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
                {/* BUG-007: Use programmatic fetch with auth header instead of bare <a href> */}
                <div className="flex flex-wrap gap-2">
                  <button
                    onClick={() => handleDownload("pdf")}
                    className="inline-flex items-center gap-1.5 rounded-md bg-green-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-700 transition-colors"
                  >
                    <Download size={12} />
                    Download PDF Report
                  </button>
                  <button
                    onClick={() => handleDownload("docx")}
                    className="inline-flex items-center gap-1.5 rounded-md border border-green-600/50 px-3 py-1.5 text-xs font-medium text-green-400 hover:bg-green-600/10 transition-colors"
                  >
                    <Download size={12} />
                    Download Word Report
                  </button>
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
            {/* File upload previews */}
            <FileUpload
              uploadedFiles={uploadedFiles}
              onFilesChange={setUploadedFiles}
              sessionUuid={uploadSessionUuid}
              onSessionUuid={setUploadSessionUuid}
              disabled={isSending || isClassifying}
              apiUrl={API_URL}
            />
            {/* Skill detection indicator */}
            {input.trim().length > 3 && (() => {
              const skill = detectSkill(input);
              if (!skill) return null;
              return (
                <div className="mb-1.5 flex items-center gap-1.5 text-[10px] text-primary/70">
                  <Zap size={10} />
                  <span>Skill detected: <span className="font-medium">{skill.name}</span></span>
                </div>
              );
            })()}
            <form onSubmit={handleSend} className="flex gap-2 sm:gap-3">
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder={user.tokens <= 0 ? "Add credits to continue..." : "Ask Mariana anything..."}
                className="min-w-0 flex-1 rounded-md border border-border bg-card px-3 py-2.5 text-sm text-foreground placeholder:text-muted-foreground/50 focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/20 sm:px-4"
                disabled={isSending || isClassifying || user.tokens <= 0}
              />
              <button
                type="submit"
                disabled={isSending || isClassifying || !input.trim() || user.tokens <= 0}
                aria-label="Send"
                className="flex shrink-0 items-center gap-2 rounded-md bg-primary px-3 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50 sm:px-4"
              >
                <Send size={14} />
              </button>
            </form>
          </div>
        </div>
      </div>

      {/* Memory panel slide-over */}
      {memoryOpen && (
        <>
          <div className="fixed inset-0 z-50 bg-black/50" onClick={() => setMemoryOpen(false)} />
          <div className="fixed inset-y-0 right-0 z-50 flex w-full max-w-sm flex-col border-l border-border bg-card shadow-2xl animate-slide-in-right">
            <div className="flex items-center justify-between border-b border-border px-4 py-3">
              <div className="flex items-center gap-2">
                <Brain size={16} className="text-primary" />
                <span className="text-sm font-medium text-foreground">Memory</span>
              </div>
              <button
                onClick={() => setMemoryOpen(false)}
                className="rounded-md p-1.5 text-muted-foreground hover:bg-secondary transition-colors"
              >
                <X size={16} />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-4 py-4">
              {memoryLoading ? (
                <div className="flex justify-center py-8">
                  <Loader2 size={16} className="animate-spin text-muted-foreground" />
                </div>
              ) : (
                <div className="space-y-6">
                  {/* Facts */}
                  <div>
                    <h3 className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/60 mb-2">
                      Stored Facts
                    </h3>
                    {memoryFacts.length === 0 ? (
                      <p className="text-xs text-muted-foreground/40">No facts stored yet</p>
                    ) : (
                      <div className="space-y-1.5">
                        {memoryFacts.map((f, i) => (
                          <div
                            key={`${f.fact}-${i}`}
                            className="group flex items-start gap-2 rounded-md border border-border/50 px-3 py-2"
                          >
                            <span className="flex-1 text-xs text-foreground leading-relaxed">{f.fact}</span>
                            <button
                              onClick={() => deleteMemoryFact(f.fact)}
                              className="shrink-0 mt-0.5 opacity-0 group-hover:opacity-100 text-red-400 hover:text-red-300 transition-opacity"
                              title="Delete"
                            >
                              <Trash2 size={11} />
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Preferences */}
                  <div>
                    <h3 className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/60 mb-2">
                      Preferences
                    </h3>
                    {Object.keys(memoryPrefs).length === 0 ? (
                      <p className="text-xs text-muted-foreground/40">No preferences stored yet</p>
                    ) : (
                      <div className="space-y-1.5">
                        {Object.entries(memoryPrefs).map(([key, value]) => (
                          <div
                            key={key}
                            className="group flex items-start gap-2 rounded-md border border-border/50 px-3 py-2"
                          >
                            <div className="flex-1 min-w-0">
                              <p className="text-[10px] font-medium text-muted-foreground">{key}</p>
                              <p className="text-xs text-foreground">{value}</p>
                            </div>
                            <button
                              onClick={() => deleteMemoryPref(key)}
                              className="shrink-0 mt-0.5 opacity-0 group-hover:opacity-100 text-red-400 hover:text-red-300 transition-opacity"
                              title="Delete"
                            >
                              <Trash2 size={11} />
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
            <div className="border-t border-border px-4 py-3">
              <p className="text-[10px] text-muted-foreground/40">
                Memory persists across research sessions to improve results.
              </p>
            </div>
          </div>
        </>
      )}

      {/* File Viewer slide-over */}
      <FileViewer
        file={viewingFile}
        onClose={() => setViewingFile(null)}
        apiUrl={API_URL}
      />
    </div>
  );
}
