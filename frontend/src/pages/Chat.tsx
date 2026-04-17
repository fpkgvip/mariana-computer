import { useState, useEffect, useRef, useCallback, useMemo } from "react";
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
  GitBranch,
  Square,
  LogOut,
  MessageSquare,
  Plus,
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
  output_pdf_path?: string | null;
  output_docx_path?: string | null;
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
  is_conversational?: boolean;
}

/** POST /api/chat/respond response */
interface ChatRespondResponse {
  reply: string;
  action: "chat" | "research";
  research_topic?: string;
  tier?: string;
  user_instructions?: string | null;
}

/** Conversation from the backend */
interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

/** A persisted message from the backend */
interface PersistedMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  type: string;
  metadata?: Record<string, unknown> | null;
  created_at: string;
}

/** Pending research plan to show in the chat before approval */
interface ResearchPlan {
  topic: string;
  tier: ClassifyResponse["tier"] | string;
  plan_summary: string;
  estimated_duration_hours: number;
  estimated_credits: number;
  /** Carried from chat/respond so handleApprovePlan can forward them */
  _userInstructions?: string;
  _convId?: string;
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
    // Use function replacement to escape quotes in URL and text, preventing attribute injection
    .replace(/\[([^\]]{1,200})\]\((https?:\/\/[^)]{1,500})\)/g,
      (_match: string, linkText: string, url: string) => {
        // Escape double quotes and backticks in both URL and link text to prevent attribute breakout
        const safeUrl = url.replace(/["'`]/g, (c) => `&#${c.charCodeAt(0)};`);
        const safeText = linkText.replace(/["'`]/g, (c) => `&#${c.charCodeAt(0)};`);
        return `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-0.5 text-primary/80 hover:text-primary text-xs underline decoration-primary/30">${safeText}<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="inline ml-0.5"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg></a>`;
      })
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

const INITIAL_QUALITY_TIER = "balanced";

export default function Chat() {
  const { user, refreshUser, logout } = useAuth();
  const navigate = useNavigate();

  // Conversation state
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const activeConversationIdRef = useRef<string | null>(null);
  activeConversationIdRef.current = activeConversationId;
  const [conversationLoading, setConversationLoading] = useState(false);

  // Messages for the currently viewed conversation
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [isClassifying, setIsClassifying] = useState(false);
  const isClassifyingRef = useRef(false);
  // BUG-D4-01: Sync ref with state — ref was set to true in handleSend but
  // never reset, permanently blocking subsequent sends.
  useEffect(() => { isClassifyingRef.current = isClassifying; }, [isClassifying]);
  const [retryPayload, setRetryPayload] = useState<{ topic: string } | null>(null);

  // Pending research plan awaiting user approval
  const [pendingPlan, setPendingPlan] = useState<ResearchPlan | null>(null);
  const [selectedTier, setSelectedTier] = useState<string>(INITIAL_QUALITY_TIER);
  const [continuousMode, setContinuousMode] = useState(false);
  const [dontKillBranches, setDontKillBranches] = useState(false);
  const [userFlowInstructions, setUserFlowInstructions] = useState("");
  // Stop button guard — prevents multiple concurrent stop requests
  const [isStopping, setIsStopping] = useState(false);

  // Investigation management
  const [investigations, setInvestigations] = useState<Investigation[]>([]);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const activeTaskIdRef = useRef<string | null>(activeTaskId);
  activeTaskIdRef.current = activeTaskId;
  const connectedTaskIdRef = useRef<string | null>(null);

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
  // BUG-C3-04 fix: Ref to always read the latest timelineSteps in
  // switchInvestigation without adding timelineSteps to its deps.
  const timelineStepsRef = useRef<TimelineStep[]>([]);
  timelineStepsRef.current = timelineSteps;

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
  const currentTokenCount = user?.tokens;

  useEffect(() => {
    if (currentTokenCount == null) return;
    if (prevTokensRef.current !== currentTokenCount && prevTokensRef.current > 0) {
      setCreditAnimating(true);
      const timer = setTimeout(() => setCreditAnimating(false), 1500);
      prevTokensRef.current = currentTokenCount;
      return () => clearTimeout(timer);
    }
    prevTokensRef.current = currentTokenCount;
  }, [currentTokenCount]);

  /* ---------------------------------------------------------------- */
  /*  Memory panel helpers                                            */
  /* ---------------------------------------------------------------- */

  const loadMemory = useCallback(async () => {
    setMemoryLoading(true);
    try {
      const token = await getAccessToken();
      if (!token) {
        toast.error("Session expired", { description: "Please sign in again." });
        return;
      }
      const res = await fetch(`${API_URL}/api/memory`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        const errorText = await res.text().catch(() => res.statusText);
        throw new Error(errorText || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setMemoryFacts(data.facts || []);
      setMemoryPrefs(data.preferences || {});
    } catch (err) {
      const message = err instanceof Error ? err.message : "Unknown error";
      toast.error("Failed to load memory", { description: message });
    } finally {
      setMemoryLoading(false);
    }
  }, []);

  const deleteMemoryFact = useCallback(async (fact: string) => {
    try {
      const token = await getAccessToken();
      if (!token) {
        toast.error("Session expired", { description: "Please sign in again." });
        return;
      }
      const res = await fetch(`${API_URL}/api/memory/facts`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ fact }),
      });
      if (!res.ok) {
        const errorText = await res.text().catch(() => res.statusText);
        throw new Error(errorText || `HTTP ${res.status}`);
      }
      setMemoryFacts((prev) => prev.filter((f) => f.fact !== fact));
      toast.success("Memory entry deleted");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Unknown error";
      toast.error("Failed to delete memory entry", { description: message });
    }
  }, []);

  const deleteMemoryPref = useCallback(async (key: string) => {
    try {
      const token = await getAccessToken();
      if (!token) {
        toast.error("Session expired", { description: "Please sign in again." });
        return;
      }
      const res = await fetch(`${API_URL}/api/memory/preferences`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      if (!res.ok) {
        const errorText = await res.text().catch(() => res.statusText);
        throw new Error(errorText || `HTTP ${res.status}`);
      }
      setMemoryPrefs((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
      toast.success("Preference deleted");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Unknown error";
      toast.error("Failed to delete preference", { description: message });
    }
  }, []);

  /* ---------------------------------------------------------------- */
  /*  Load investigations from backend API (source of truth)          */
  /* ---------------------------------------------------------------- */

  // BUG-R15-02: Depend on user.id (stable) not user (new object on every refreshUser())
  const userId = user?.id;
  useEffect(() => {
    if (!userId) return;
    const loadInvestigations = async () => {
      // BUG-011 fix: Use the backend API as the source of truth for investigation
      // status. Previously loaded from Supabase which could have stale PENDING
      // statuses if the user closed the tab before the investigation completed.
      try {
        const session = await supabase.auth.getSession();
        const token = session.data.session?.access_token;
        if (!token) throw new Error("No auth token");
        const res = await fetch(`${API_URL}/api/investigations`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        const items = json.items ?? json;
        if (Array.isArray(items)) {
          // Map backend fields to frontend Investigation shape
          const mapped = items.map((inv: Record<string, unknown>) => ({
            task_id: inv.id ?? inv.task_id,
            topic: inv.topic,
            status: inv.status,
            created_at: inv.created_at,
            duration_hours: inv.duration_hours ?? 0,
            budget_usd: inv.budget_usd ?? 0,
            output_pdf_path: inv.output_pdf_path ?? null,
            output_docx_path: inv.output_docx_path ?? null,
          }));
          setInvestigations(mapped as Investigation[]);
          // Also sync statuses back to Supabase for consistency
          for (const inv of mapped) {
            if (inv.status !== "PENDING") {
              supabase
                .from("investigations")
                .upsert(
                  {
                    task_id: inv.task_id,
                    topic: inv.topic,
                    status: inv.status,
                    user_id: userId,
                    budget_usd: inv.budget_usd,
                    duration_hours: inv.duration_hours,
                    output_pdf_path: inv.output_pdf_path,
                    output_docx_path: inv.output_docx_path,
                  },
                  { onConflict: "task_id" }
                )
                .then(({ error }) => {
                  if (error) console.warn("[Chat] Supabase sync error:", error.message);
                })
                .catch(() => {});
            }
          }
          return;
        }
      } catch (err) {
        console.warn("[Chat] Backend API unavailable, falling back to Supabase:", err);
      }
      // Fallback: load from Supabase if backend API is unavailable
      const { data, error } = await supabase
        .from("investigations")
        .select("task_id, topic, status, created_at, duration_hours, budget_usd, output_pdf_path, output_docx_path")
        .eq("user_id", userId)
        .order("created_at", { ascending: false });
      if (error) {
        console.error("[Chat] Failed to load investigations:", error.message);
        toast.error("Failed to load investigations", { description: error.message });
        return;
      }
      setInvestigations((data as Investigation[]) ?? []);
    };
    loadInvestigations();
  }, [userId]);

  /* ---------------------------------------------------------------- */
  /*  Load conversations from backend                                 */
  /* ---------------------------------------------------------------- */

  const loadConversations = useCallback(async () => {
    try {
      const token = await getAccessToken();
      if (!token) return;
      const res = await fetch(`${API_URL}/api/conversations`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return;
      const data = await res.json();
      setConversations(data.items ?? []);
    } catch (err) {
      console.warn("[Chat] Failed to load conversations:", err);
    }
  }, []);

  useEffect(() => {
    if (!userId) return;
    loadConversations();
  }, [userId, loadConversations]);

  /* ---------------------------------------------------------------- */
  /*  Load a conversation's messages from backend                     */
  /* ---------------------------------------------------------------- */

  const loadConversationMessages = useCallback(async (conversationId: string) => {
    setConversationLoading(true);
    try {
      const token = await getAccessToken();
      if (!token) { setConversationLoading(false); return; }
      const res = await fetch(`${API_URL}/api/conversations/${conversationId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) { setConversationLoading(false); return; }
      // Guard against stale responses from rapid conversation switching
      if (activeConversationIdRef.current !== conversationId) { setConversationLoading(false); return; }
      const data = await res.json();
      const restored: Message[] = (data.messages || []).map((m: PersistedMessage) => ({
        role: m.role as Message["role"],
        content: m.content,
        type: (m.type || "text") as Message["type"],
        _id: m.id,
      }));
      setMessages(restored);

      // If there are linked investigations, find them and set the latest running one as active
      if (data.investigations && data.investigations.length > 0) {
        const runningInv = investigations.find(
          (inv) => data.investigations.includes(inv.task_id) && (inv.status === "RUNNING" || inv.status === "PENDING")
        );
        if (runningInv) {
          setActiveTaskId(runningInv.task_id);
          setIsSending(true);
          // Restore timeline steps from in-memory cache if available
          const cachedTimeline = timelineStoreRef.current[runningInv.task_id];
          if (cachedTimeline && cachedTimeline.length > 0) {
            setTimelineSteps(cachedTimeline);
          }
          const token2 = await getAccessToken();
          if (token2) {
            startTimer(runningInv.created_at);
            startSSE(runningInv.task_id, token2);
          }
        } else {
          // Set the most recent investigation as active (for download buttons etc.)
          const lastInv = investigations.find((inv) => data.investigations.includes(inv.task_id));
          if (lastInv) {
            setActiveTaskId(lastInv.task_id);
            // Restore timeline steps from cache
            const cachedTimeline = timelineStoreRef.current[lastInv.task_id];
            if (cachedTimeline && cachedTimeline.length > 0) {
              setTimelineSteps(cachedTimeline);
            }
          }
        }
      }
    } catch (err) {
      console.warn("[Chat] Failed to load conversation messages:", err);
    } finally {
      setConversationLoading(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [investigations]);

  /* ---------------------------------------------------------------- */
  /*  Save a message to the backend                                   */
  /* ---------------------------------------------------------------- */

  const persistMessage = useCallback(async (
    conversationId: string,
    role: string,
    content: string,
    type: string = "text",
  ) => {
    try {
      const token = await getAccessToken();
      if (!token) return;
      await fetch(`${API_URL}/api/conversations/messages`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: conversationId, role, content, type }),
      });
    } catch (err) {
      console.warn("[Chat] Failed to persist message:", err);
    }
  }, []);

  /* ---------------------------------------------------------------- */
  /*  Create a new conversation                                       */
  /* ---------------------------------------------------------------- */

  const createConversation = useCallback(async (title?: string): Promise<string | null> => {
    try {
      const token = await getAccessToken();
      if (!token) return null;
      const res = await fetch(`${API_URL}/api/conversations`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ title: title || "New conversation" }),
      });
      if (!res.ok) return null;
      const data = await res.json();
      const newConv: Conversation = {
        id: data.id,
        title: data.title,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      setConversations((prev) => [newConv, ...prev]);
      return data.id;
    } catch (err) {
      console.warn("[Chat] Failed to create conversation:", err);
      return null;
    }
  }, []);

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

  // BUG-FIX-09: Accept optional origin time so reconnects show correct elapsed
  const startTimer = useCallback((fromTime?: string | number) => {
    if (timerRef.current) clearInterval(timerRef.current);
    const origin = fromTime ? new Date(fromTime).getTime() : Date.now();
    startTimeRef.current = origin;
    setElapsedSeconds(Math.max(0, Math.floor((Date.now() - origin) / 1000)));
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
    setElapsedSeconds(0);
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

  const processStructuredEvent = useCallback((event: StructuredEvent, taskId?: string) => {
    if (taskId && activeTaskIdRef.current !== taskId) {
      return;
    }

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

    // For file_attached events, also add a message so it shows in the chat.
    // BUG-F2-05: Store the backend-provided url field if present so the renderer
    // can prefer it over the constructed API path (forward-compat with CDN URLs).
    if (event.type === "file_attached" && event.filename) {
      appendMessage({
        role: "system",
        content: JSON.stringify({
          type: "file_attached",
          filename: event.filename,
          size: event.size ?? 0,
          mime: event.mime,
          url: event.url ?? null,
        }),
        type: "status",
        id: `file-${event.filename}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        _id: makeMessageId(),
      });
    }

    // graph_update events — acknowledge with a timeline status entry.
    // The InvestigationGraph page polls /graph independently so no state merge needed here.
    if (event.type === "graph_update") {
      const nodeCount = Array.isArray(event.nodes)
        ? event.nodes.length
        : null;
      appendMessage({
        role: "system",
        content: nodeCount != null
          ? `Knowledge graph updated: ${nodeCount} node${nodeCount !== 1 ? "s" : ""}`
          : "Knowledge graph updated.",
        type: "status",
        id: `graph-update-${Date.now()}`,
        _id: makeMessageId(),
      });
    }

    // For text events that carry actual content (non-subagent), also append to chat
    // so the user sees the assistant's findings in the message stream.
    // SubAgent announcements are timeline-only (they are operational noise, not findings).
    if (event.type === "text") {
      const textContent = event.content || event.message || "";
      const isSubAgent = textContent.startsWith("[SubAgent]");
      if (!isSubAgent && textContent.trim()) {
        appendMessage({
          role: "assistant",
          content: textContent,
          type: "text",
          _id: makeMessageId(),
        });
      }
    }

    // BUG-F2-01: Handle warning events — show inline in chat as a caution message.
    if (event.type === "warning") {
      const warnMsg = event.message || "";
      if (warnMsg.trim()) {
        appendMessage({
          role: "system",
          content: `⚠️ ${warnMsg}`,
          type: "status",
          // BUG-F4-01: Use content-based id so identical warnings are deduplicated
          // on SSE reconnect. Date.now() produces a unique id on every call, making
          // seenStatusIds dedup ineffective and flooding the chat when the stream
          // reconnects and re-emits previously seen warning events.
          id: `warning-${warnMsg}`,
          _id: makeMessageId(),
        });
      }
    }

    // BUG-F2-01: Handle branch_update events — show a brief status line.
    if (event.type === "branch_update") {
      const branchId = event.branch_id ?? "";
      const branchStatus = event.status ?? "";
      if (branchId || branchStatus) {
        appendMessage({
          role: "system",
          content: `Branch${branchId ? ` ${branchId}` : ""}: ${branchStatus || "updated"}.`,
          type: "status",
          id: `branch-${branchId}-${branchStatus}`,
          _id: makeMessageId(),
        });
      }
    }

    // BUG-F2-01: Handle checkpoint events — show summary in chat.
    if (event.type === "checkpoint") {
      const summary = event.summary ?? "";
      const sourcesCount = event.sources_count;
      const detail = [
        summary,
        sourcesCount != null ? `${sourcesCount} source${sourcesCount !== 1 ? "s" : ""}` : null,
      ].filter(Boolean).join(" · ");
      if (detail) {
        appendMessage({
          role: "system",
          content: `Checkpoint: ${detail}`,
          type: "status",
          // BUG-F4-02: Use content-based id so identical checkpoint messages are
          // deduplicated on SSE reconnect. Same root cause as BUG-F4-01.
          id: `checkpoint-${detail}`,
          _id: makeMessageId(),
        });
      }
    }

    // For cost_update events at investigation completion, show credit summary.
    // spent_usd already includes the 20% markup per the backend spec;
    // credits are 100 credits per USD so we do NOT multiply by 1.20 again.
    if (event.type === "cost_update" && event.spent_usd != null) {
      const creditsUsed = Math.round(event.spent_usd * 100);
      appendMessage({
        role: "system",
        content: JSON.stringify({
          type: "cost_summary",
          credits_used: creditsUsed,
          spent_usd: event.spent_usd,
          budget_usd: event.budget_usd,
        }),
        type: "status",
        id: `cost-final-${taskId ?? Date.now()}`,
        _id: makeMessageId(),
      });
    }
  }, [appendMessage]);

  /* ---------------------------------------------------------------- */
  /*  Update investigation status locally and in Supabase             */
  /* ---------------------------------------------------------------- */

  const updateInvestigationStatus = useCallback(
    async (
      taskId: string,
      status: InvestigationStatus,
      extra?: { output_pdf_path?: string | null; output_docx_path?: string | null },
    ) => {
      setInvestigations((prev) =>
        prev.map((inv) => (inv.task_id === taskId ? { ...inv, status, ...extra } : inv))
      );
      await supabase
        .from("investigations")
        .update({ status })
        .eq("task_id", taskId)
        .then(({ error }) => {
          if (error) console.error("[Chat] Failed to update investigation status:", error.message);
        })
        // BUG-R15-03: Catch network-level rejections to prevent unhandled promise rejection
        .catch((err) => console.error("[Chat] Failed to update investigation status (network):", err));

      // BUG-R13-01: When completing, re-fetch output paths from Supabase so the
      // DOCX download button is correctly enabled even when the SSE event didn't
      // carry the paths (only the poll response does).
      if (status === "COMPLETED" && !extra?.output_docx_path) {
        const { data: row } = await supabase
          .from("investigations")
          .select("output_pdf_path, output_docx_path")
          .eq("task_id", taskId)
          .single();
        if (row && (row.output_pdf_path || row.output_docx_path)) {
          setInvestigations((prev) =>
            prev.map((inv) =>
              inv.task_id === taskId
                ? { ...inv, output_pdf_path: row.output_pdf_path, output_docx_path: row.output_docx_path }
                : inv
            )
          );
        }
      }
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
    connectedTaskIdRef.current = null;
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
    // BUG-F3-02: Reset isStopping when stopping all connections (e.g. on investigation
    // switch). Without this, the Stop button on the new investigation would briefly
    // appear stuck in "Stopping..." / disabled state if the user switched while a
    // stop request was in-flight. The fetch's `finally` block would eventually correct
    // it, but resetting here makes the transition instant and avoids the visual glitch.
    setIsStopping(false);
  }, [stopConnectionsOnly]);

  /* ---------------------------------------------------------------- */
  /*  Polling fallback                                                */
  /* ---------------------------------------------------------------- */

  const startPolling = useCallback(
    (taskId: string, _initialToken: string) => {
      connectedTaskIdRef.current = taskId;
      const poll = async () => {
        if (connectedTaskIdRef.current !== taskId || activeTaskIdRef.current !== taskId) {
          if (pollIntervalRef.current) {
            clearInterval(pollIntervalRef.current);
            pollIntervalRef.current = null;
          }
          return;
        }
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
              id: `rate-limit-${taskId}`,
              _id: makeMessageId(),
            });
            return;
          }

          if (!res.ok) return;

          // BUG-R2-02: Backend returns TaskSummary — use "id", "current_state" not "task_id", "status_message"
          const data: InvestigationPollResponse = await res.json();

          // Show current_state as a progress message — map internal states to user-friendly text
          if (data.current_state) {
            const STATE_LABELS: Record<string, string | null> = {
              INIT: null,           // suppress — "Initializing" already shown
              HALT: null,           // suppress — completion message shown separately
              COMPLETED: null,      // suppress — handled by status === "COMPLETED" branch
              HALTED: null,         // suppress — handled by status === "HALTED" branch
              PENDING: null,        // suppress — not useful
              RUNNING: null,        // suppress — not useful
              SEARCHING: "Searching the web...",
              EVALUATING: "Evaluating findings...",
              REPORTING: "Generating report...",
              DEEP_DIVE: "Conducting deep dive...",
              CHECKPOINT: "Checkpoint — reviewing progress...",
              PIVOT: "Adjusting research direction...",
              TRIBUNAL: "Running adversarial review...",
              SKEPTIC_REVIEW: "Skeptic review in progress...",
            };
            const stateKey = data.current_state.toUpperCase();
            const label = stateKey in STATE_LABELS ? STATE_LABELS[stateKey] : data.current_state;
            if (label) {
              appendMessage({
                role: "system",
                content: label,
                type: "status",
                id: `poll-state-${data.current_state}`,
                _id: makeMessageId(),
              });
            }
          }

          if (data.status === "RUNNING") {
            updateInvestigationStatus(taskId, "RUNNING");
          }

          if (data.status === "COMPLETED") {
            updateInvestigationStatus(taskId, "COMPLETED", {
              output_pdf_path: data.output_pdf_path,
              output_docx_path: data.output_docx_path,
            });
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

            // Fetch executive summary for inline display (polling path)
            (async () => {
              try {
                const sumToken = await getAccessToken();
                if (!sumToken) return;
                const sumRes = await fetch(`${API_URL}/api/intelligence/${taskId}/executive-summary`, {
                  headers: { Authorization: `Bearer ${sumToken}` },
                });
                if (!sumRes.ok) return;
                const sumData = await sumRes.json();
                const execSummary = sumData?.summary?.executive_summary
                  || sumData?.summary?.one_liner
                  || sumData?.summary?.summary
                  || (typeof sumData?.summary === "string" ? sumData.summary : null);
                if (execSummary && typeof execSummary === "string" && execSummary.trim()) {
                  appendMessage({
                    role: "assistant",
                    content: execSummary.trim(),
                    type: "text",
                    _id: makeMessageId(),
                  });
                }
              } catch {
                // Non-critical — user can still download the full report
              }
            })();
          } else if (data.status === "FAILED" || data.status === "HALTED") {
            updateInvestigationStatus(taskId, data.status as InvestigationStatus);
            const errMsg = data.error ?? `The investigation ${data.status.toLowerCase()}. Please try again.`;
            appendMessage({
              role: "assistant",
              content: errMsg,
              type: "text",
              _id: makeMessageId(),
            });
            toast.error(`Investigation ${data.status.toLowerCase()}`, { description: errMsg });
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
    async (taskId: string, token: string) => {
      connectedTaskIdRef.current = taskId;
      // SEC-E3-R1-01: Mint a short-lived stream token instead of exposing
      // the full JWT in the SSE query string. The stream token is HMAC-signed,
      // bound to this specific task, and expires in 2 minutes.
      let streamToken = token; // Fallback to JWT if mint fails
      try {
        const res = await fetch(`${API_URL}/api/investigations/${taskId}/stream-token`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        });
        if (res.ok) {
          const data = await res.json();
          streamToken = data.stream_token;
        }
      } catch {
        // Fallback: use JWT directly (backward compat with older backends)
      }

      const url = `${API_URL}/api/investigations/${taskId}/logs?token=${encodeURIComponent(streamToken)}`;

      try {
        const es = new EventSource(url);
        eventSourceRef.current = es;

        // BUG-002: Guard against multiple fallback polling loops
        let hasFailedOver = false;

        // BUG-R5-01: The main handler processes both unnamed "message" events
        // (es.onmessage) and named "log" events (es.addEventListener("log")).
        // The backend Redis pub/sub path emits structured JSON as event type "log",
        // which es.onmessage silently ignores. Without the named listener, all
        // real-time progress events (text, status_change, step_start, etc.) from
        // the primary Redis path were dropped.
        const handleLogEvent = (event: MessageEvent) => {
          if (connectedTaskIdRef.current !== taskId || activeTaskIdRef.current !== taskId) {
            return;
          }
          try {
            const parsed = JSON.parse(event.data);

            // Check if this is a structured progress event (has a "type" field)
            const eventType = parsed.type as string | undefined;
            if (eventType && [
              "step_start", "step_complete", "step_error", "status_change",
              "file_attached", "cost_update", "hypothesis_update", "text",
              "graph_update",
              // BUG-F2-01: branch_update, checkpoint, and warning were missing —
              // they fell through to the legacy handler which rendered raw JSON
              // as a status message instead of routing through processStructuredEvent.
              "branch_update", "checkpoint", "warning",
            ].includes(eventType)) {
              processStructuredEvent(parsed as StructuredEvent, taskId);

              // BUG-FIX-07: Differentiate HALT (normal completion) from HALTED (abnormal)
              if (eventType === "status_change") {
                const state = parsed.state as string;
                if (state === "HALT" || state === "COMPLETED") {
                  // BUG-R14-02: Forward output paths from SSE payload if available
                  updateInvestigationStatus(taskId, "COMPLETED", {
                    output_pdf_path: parsed.output_pdf_path as string | null | undefined,
                    output_docx_path: parsed.output_docx_path as string | null | undefined,
                  });
                  appendMessage({
                    role: "system",
                    content: "Investigation complete. Reports are ready for download.",
                    type: "status",
                    id: `completed-${taskId}`,
                    _id: makeMessageId(),
                  });
                  stopAllConnections();
                  refreshUser();

                  // Fetch executive summary for inline display
                  (async () => {
                    try {
                      const sumToken = await getAccessToken();
                      if (!sumToken) return;
                      const sumRes = await fetch(`${API_URL}/api/intelligence/${taskId}/executive-summary`, {
                        headers: { Authorization: `Bearer ${sumToken}` },
                      });
                      if (!sumRes.ok) return;
                      const sumData = await sumRes.json();
                      const execSummary = sumData?.summary?.executive_summary
                        || sumData?.summary?.one_liner
                        || sumData?.summary?.summary
                        || (typeof sumData?.summary === "string" ? sumData.summary : null);
                      if (execSummary && typeof execSummary === "string" && execSummary.trim()) {
                        appendMessage({
                          role: "assistant",
                          content: execSummary.trim(),
                          type: "text",
                          _id: makeMessageId(),
                        });
                      }
                    } catch {
                      // Non-critical — user can still download the full report
                    }
                  })();
                } else if (state === "HALTED") {
                  updateInvestigationStatus(taskId, "HALTED");
                  appendMessage({
                    role: "system",
                    content: "Investigation was halted. Some results may be incomplete.",
                    type: "error",
                    id: `halted-${taskId}`,
                    _id: makeMessageId(),
                  });
                  stopAllConnections();
                  refreshUser();
                }
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
              // BUG-R14-05: Forward output paths from legacy SSE payload if available
              updateInvestigationStatus(taskId, "COMPLETED", {
                output_pdf_path: parsed.output_pdf_path as string | null | undefined,
                output_docx_path: parsed.output_docx_path as string | null | undefined,
              });
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
            } else if (status === "FAILED" || status === "HALTED") {
              updateInvestigationStatus(taskId, status as InvestigationStatus);
              appendMessage({
                role: "assistant",
                content: parsed.error || `The investigation ${status.toLowerCase()}.`,
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

        // Wire up the handler for both unnamed "message" events AND named "log" events.
        es.onmessage = handleLogEvent;
        es.addEventListener("log", handleLogEvent);

        // BUG-R2-08: Register named event listeners for server-emitted event types.
        // The backend also emits: "done", "ping", "state_change", "error".

        es.addEventListener("done", (event: MessageEvent) => {
          if (connectedTaskIdRef.current !== taskId || activeTaskIdRef.current !== taskId) {
            return;
          }
          try {
            const parsed = JSON.parse(event.data);
            const finalStatus = (parsed.final_status || parsed.status) as InvestigationStatus;
            if (finalStatus === "COMPLETED") {
              // BUG-R14-03: Forward output paths from done event payload if available
              updateInvestigationStatus(taskId, "COMPLETED", {
                output_pdf_path: parsed.output_pdf_path as string | null | undefined,
                output_docx_path: parsed.output_docx_path as string | null | undefined,
              });
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
              updateInvestigationStatus(taskId, finalStatus as InvestigationStatus);
              appendMessage({
                role: "assistant",
                content: parsed.error || `Investigation ${finalStatus.toLowerCase()}.`,
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
          if (connectedTaskIdRef.current !== taskId || activeTaskIdRef.current !== taskId) {
            return;
          }
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
          } catch {
            // Ignore malformed state_change payloads and keep the stream alive.
          }
        });

        // Named "error" event from the server (distinct from connection errors via es.onerror)
        es.addEventListener("error", (event: MessageEvent) => {
          if (connectedTaskIdRef.current !== taskId || activeTaskIdRef.current !== taskId) {
            return;
          }
          try {
            const parsed = JSON.parse(event.data);
            const errMsg = parsed.error || parsed.message || "Stream error";
            appendMessage({
              role: "assistant",
              content: errMsg,
              type: "error",
              _id: makeMessageId(),
            });
            stopAllConnections();
            refreshUser();
          } catch {
            // Not a server-named error — let es.onerror handle connection errors
          }
        });

        es.addEventListener("ping", () => {
          // keepalive — intentionally ignored
        });

        // BUG-002: hasFailedOver flag prevents multiple concurrent polling loops
        es.onerror = async () => {
          if (connectedTaskIdRef.current !== taskId || activeTaskIdRef.current !== taskId) {
            return;
          }
          if (hasFailedOver) return;
          hasFailedOver = true;
          console.warn("[Chat] SSE connection error, falling back to polling.");
          es.close();
          eventSourceRef.current = null;
          // BUG-R1-02: Fetch fresh token for polling fallover — the SSE token
          // may have been created some time ago and could be near expiry.
          const freshToken = await getAccessToken();
          if (!freshToken) {
            // Token refresh failed — session expired
            stopAllConnections();
            toast.error("Session expired", { description: "Please sign in again." });
            navigate("/login");
            return;
          }
          // BUG-R2-17: Guard against starting a polling loop if the component
          // unmounted during the async getAccessToken() call.
          if (pollIntervalRef.current === null) {
            startPolling(taskId, freshToken);
          }
        };
      } catch {
        console.warn("[Chat] SSE not available, using polling.");
        // Pass token through for initial call; poll() will refresh on each tick
        startPolling(taskId, token);
      }
    },
    [appendMessage, navigate, processStructuredEvent, refreshUser, startPolling, stopAllConnections, updateInvestigationStatus]
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
      // BUG-C3-04 fix: Use ref instead of closure value to avoid stale data.
      if (activeTaskId) {
        timelineStoreRef.current[activeTaskId] = [...timelineStepsRef.current];
      }

      // Stop any active connections
      stopAllConnections();
      setElapsedSeconds(0);

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
          if (activeTaskIdRef.current !== taskId) {
            return;
          }
          if (token) {
            startTimer(inv.created_at);
            startSSE(taskId, token);
          } else {
            setIsSending(false);
            toast.error("Session expired", { description: "Please sign in again." });
            navigate("/login");
          }
        }).catch(() => {
          setIsSending(false);
        });
      }
    },
    // messages and timelineSteps removed from deps — using refs instead
    [activeTaskId, investigations, navigate, stopAllConnections, startSSE, startTimer]
  );

  /* ---------------------------------------------------------------- */
  /*  Classify topic and start investigation flow                     */
  /* ---------------------------------------------------------------- */

  /**
   * Entry point: user hits send.
   *
   * Chat-first architecture:
   * 1. Every message goes to /api/chat/respond first
   * 2. The AI decides: conversation (reply directly) or research (launch investigation)
   * 3. For research: quick/standard auto-launch, deep shows approval card
   */
  const handleSend = useCallback(async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (isSending || isClassifying || isClassifyingRef.current) return;

    const topic = input.trim() || retryPayload?.topic || "";
    if (!topic) return;

    // Lock IMMEDIATELY via ref to prevent rapid double-sends (React state
    // batching means useState won't be visible to a second click yet)
    isClassifyingRef.current = true;
    setRetryPayload(null);
    setInput("");
    setIsClassifying(true);

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

    // ── Conversation management: create or continue ───────────────
    let convId = activeConversationIdRef.current;
    if (!convId) {
      // No active conversation — create one. Auto-title from the user's message.
      convId = await createConversation(topic.slice(0, 60));
      if (convId) {
        setActiveConversationId(convId);
      }
    }

    const newMessages: Message[] = [
      { role: "user", content: topic, type: "text", _id: makeMessageId() },
    ];
    // Append to existing messages (continuing a conversation)
    setMessages((prev) => [...prev, ...newMessages]);

    // Persist the user message to the backend
    if (convId) {
      persistMessage(convId, "user", topic, "text");
      // Move this conversation to top of sidebar (most recently active)
      setConversations((prev) => {
        const idx = prev.findIndex((c) => c.id === convId);
        if (idx <= 0) return prev; // already at top or not found
        const updated = [...prev];
        const [moved] = updated.splice(idx, 1);
        moved.updated_at = new Date().toISOString();
        return [moved, ...updated];
      });
    }

    // Get auth token
    const token = await getAccessToken();
    if (!token) {
      toast.error("Not authenticated", {
        description: "Please sign in.",
      });
      navigate("/login");
      return;
    }

    setIsClassifying(true);

    try {
      // ── Step 1: Ask the AI how to handle this message ─────────────────
      const chatRes = await fetch(`${API_URL}/api/chat/respond`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ message: topic, conversation_id: convId }),
      });

      if (chatRes.status === 401) {
        toast.error("Session expired", { description: "Please sign in again." });
        setIsClassifying(false);
        navigate("/login");
        return;
      }

      if (!chatRes.ok) {
        // Chat endpoint failed — show fallback plan for approval
        console.warn("[Chat] chat/respond failed, showing fallback plan.");
        setIsClassifying(false);
        setPendingPlan({
          topic,
          tier: "standard",
          plan_summary: `Standard investigation: ${topic}`,
          estimated_duration_hours: 0.1,
          estimated_credits: 100,
          _convId: convId || undefined,
        });
        return;
      }

      const chatData: ChatRespondResponse = await chatRes.json();
      setIsClassifying(false);

      // ── Step 2: Route based on AI decision ────────────────────────
      if (chatData.action === "chat") {
        // Pure conversation — show the reply, no investigation
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: chatData.reply, type: "text", _id: makeMessageId() },
        ]);
        // Persist the assistant reply
        if (convId) {
          persistMessage(convId, "assistant", chatData.reply, "text");
        }
        return;
      }

      // action === "research" — AI wants to launch an investigation
      const researchTopic = chatData.research_topic || topic;
      const tier = chatData.tier || "standard";

      // Show the AI's message about what it will research
      if (chatData.reply) {
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: chatData.reply, type: "text", _id: makeMessageId() },
        ]);
        // Persist the assistant reply
        if (convId) {
          persistMessage(convId, "assistant", chatData.reply, "text");
        }
      }

      // Always show the research plan for human approval before starting
      const tierMeta: Record<string, { label: string; credits: number; duration: string; hours: number }> = {
        instant: { label: "Instant lookup", credits: 5, duration: "~10 seconds", hours: 0.003 },
        quick:   { label: "Quick research", credits: 20, duration: "~30 seconds", hours: 0.01 },
        standard:{ label: "Standard investigation", credits: 100, duration: "~5 minutes", hours: 0.1 },
        deep:    { label: "Deep investigation", credits: 500, duration: "15–45 minutes", hours: 0.5 },
      };
      const meta = tierMeta[tier] || tierMeta.standard;
      setPendingPlan({
        topic: researchTopic,
        tier,
        plan_summary: `${meta.label}: ${researchTopic}`,
        estimated_duration_hours: meta.hours,
        estimated_credits: meta.credits,
        _userInstructions: chatData.user_instructions || undefined,
        _convId: convId || undefined,
      });
    } catch (err) {
      setIsClassifying(false);
      console.warn("[Chat] Chat error, showing fallback plan:", err);
      // Even on error, show a plan for approval — never auto-launch without consent
      setPendingPlan({
        topic,
        tier: "standard",
        plan_summary: `Standard investigation: ${topic}`,
        estimated_duration_hours: 0.1,
        estimated_credits: 100,
        _convId: convId || undefined,
      });
    }
  // BUG-C3-06 fix: startInvestigation accessed via startInvestigationRef.current
  // so handleSend always calls the latest version (avoids stale uploadSessionUuid).
  }, [isSending, isClassifying, input, retryPayload, activeTaskId, stopConnectionsOnly, navigate, createConversation, persistMessage]);

  /* ---------------------------------------------------------------- */
  /*  Start investigation (after classify or after plan approval)     */
  /* ---------------------------------------------------------------- */

  const startInvestigation = useCallback(async (
    topic: string,
    token: string,
    planApproved: boolean,
    overrideTier?: string,
    chatUserInstructions?: string,
    conversationId?: string,
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
      // Merge user instructions: chat-extracted instructions + advanced panel instructions.
      // Chat instructions come from the AI parsing the user's message (e.g. "focus on X").
      // Panel instructions come from the advanced settings textarea.
      // Both are combined so the AI sees everything the user wants.
      const mergedInstructions = [
        chatUserInstructions || "",
        userFlowInstructions || "",
      ].filter(Boolean).join("\n\n");

      const requestBody: Record<string, unknown> = {
        topic,
        plan_approved: planApproved,
        quality_tier: selectedTier,
        continuous_mode: continuousMode,
        dont_kill_branches: dontKillBranches,
        user_flow_instructions: mergedInstructions,
        ...(overrideTier ? { tier: overrideTier } : {}),
        ...(conversationId ? { conversation_id: conversationId } : {}),
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
          id: `credits-insufficient`,
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
          id: `rate-limit-submit`,
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
            ...(conversationId ? { conversation_id: conversationId } : {}),
          })
          .then(({ error }) => {
            if (error) console.error("[Chat] Failed to persist investigation:", error.message);
          })
          // BUG-R15-03: Catch network-level rejections to prevent unhandled promise rejection
          .catch((err) => console.error("[Chat] Failed to persist investigation (network):", err));
      }

      appendMessage({
        role: "system",
        content: `Investigation started (ID: ${taskId}). Monitoring progress...`,
        type: "status",
        id: `started-${taskId}`,
        _id: makeMessageId(),
      });

      // Refresh credit balance — reservation was deducted at submit time
      refreshUser();

      // Clear upload state after investigation starts
      setUploadedFiles([]);
      setUploadSessionUuid(null);

      // Start timer and SSE
      startTimer(newInvestigation.created_at);
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
  }, [user, selectedTier, continuousMode, dontKillBranches, userFlowInstructions, uploadSessionUuid, appendMessage, refreshUser, startTimer, startSSE, navigate]);

  // BUG-C3-06 fix: Ref to hold the latest startInvestigation so handleSend
  // (defined before startInvestigation) always calls the current version,
  // avoiding stale closures when uploadSessionUuid changes.
  const startInvestigationRef = useRef(startInvestigation);
  startInvestigationRef.current = startInvestigation;

  /* ---------------------------------------------------------------- */
  /*  Approve research plan                                           */
  /* ---------------------------------------------------------------- */

  const handleApprovePlan = useCallback(async () => {
    if (!pendingPlan || isClassifying || isSending) return;
    // BUG-F3-01: Guard against rapid double-clicks. React batches state updates,
    // so `setPendingPlan(null)` is NOT synchronously visible to a second click
    // that fires before re-render. Setting isClassifying=true here gives us a
    // synchronous-in-the-next-tick guard AND lets the Approve button's `disabled`
    // prop block the second click at the UI layer.
    setIsClassifying(true);
    const planToApprove = pendingPlan;

    try {
      const token = await getAccessToken();
      if (!token) {
        toast.error("Not authenticated", { description: "Please sign in again." });
        navigate("/login");
        return;
      }

      setPendingPlan(null);
      await startInvestigationRef.current(
        planToApprove.topic,
        token,
        true,
        planToApprove.tier,
        planToApprove._userInstructions,
        planToApprove._convId || activeConversationIdRef.current || undefined,
      );
    } finally {
      setIsClassifying(false);
    }
  }, [pendingPlan, isClassifying, isSending, navigate]);

  /* ---------------------------------------------------------------- */
  /*  Cancel research plan                                            */
  /* ---------------------------------------------------------------- */

  const handleCancelPlan = useCallback(() => {
    setPendingPlan(null);
    // Remove the plan from the chat — keep messages from conversation history
    setTimelineSteps([]);
    setActiveTaskId(null);
    setUploadedFiles([]);
    setUploadSessionUuid(null);
    seenStatusIds.current.clear();
    setIsSending(false);
    setIsClassifying(false);
    setIsStopping(false); // BUG-F3-02: also clear stop guard on cancel
    setSelectedTier(INITIAL_QUALITY_TIER);
    setContinuousMode(false);
    setDontKillBranches(false);
    setUserFlowInstructions("");
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
  const latestAnnouncement = useMemo(() => {
    const lastNonUserMessage = [...messages].reverse().find((msg) => msg.role !== "user");
    if (lastNonUserMessage?.content) return lastNonUserMessage.content;
    if (pendingPlan) return "Research plan ready for review.";
    if (isClassifying) return "Analyzing your question.";
    if (isSending) return "Mariana is researching.";
    return "";
  }, [messages, pendingPlan, isClassifying, isSending]);

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

        {/* New chat button */}
        <div className="px-4 pt-4">
          <button
            onClick={() => {
              if (activeTaskId && messagesRef.current.length > 0) {
                messageStoreRef.current[activeTaskId] = [...messagesRef.current];
              }
              if (activeTaskId) {
                timelineStoreRef.current[activeTaskId] = [...timelineStepsRef.current];
              }
              stopAllConnections();
              setActiveTaskId(null);
              setActiveConversationId(null);
              setMessages([]);
              setTimelineSteps([]);
              setPendingPlan(null);
              setSelectedTier(INITIAL_QUALITY_TIER);
              setContinuousMode(false);
              setDontKillBranches(false);
              setUserFlowInstructions("");
              setUploadedFiles([]);
              setUploadSessionUuid(null);
              seenStatusIds.current.clear();
              setSidebarOpen(false);
            }}
            className="w-full flex items-center justify-center gap-1.5 rounded-md border border-border px-3 py-2 text-xs text-foreground hover:bg-secondary transition-colors"
          >
            <Plus size={13} />
            New Chat
          </button>
        </div>

        {/* Conversation list */}
        <div className="flex-1 overflow-y-auto px-4 py-4">
          {conversations.length === 0 && (
            <p className="text-xs text-muted-foreground/60 py-2 text-center">No conversations yet</p>
          )}
          <div className="space-y-0.5">
            {conversations.map((conv) => {
              const isActive = activeConversationId === conv.id;
              return (
                <div
                  key={conv.id}
                  className={`group relative w-full rounded-md text-left text-xs transition-colors ${
                    isActive
                      ? "bg-secondary text-foreground ring-1 ring-primary/30"
                      : "text-muted-foreground hover:bg-secondary/50 hover:text-foreground"
                  }`}
                >
                  <button
                    onClick={() => {
                      if (isActive) return;
                      // Save current state
                      if (activeTaskId && messagesRef.current.length > 0) {
                        messageStoreRef.current[activeTaskId] = [...messagesRef.current];
                      }
                      if (activeTaskId) {
                        timelineStoreRef.current[activeTaskId] = [...timelineStepsRef.current];
                      }
                      stopAllConnections();
                      setActiveTaskId(null);
                      setActiveConversationId(conv.id);
                      setIsSending(false);
                      setMessages([]);
                      setTimelineSteps([]);
                      setPendingPlan(null);
                      setUploadedFiles([]);
                      setUploadSessionUuid(null);
                      setElapsedSeconds(0);
                      setConversationLoading(true); // Set BEFORE async call to prevent welcome screen flash
                      seenStatusIds.current.clear();
                      setSidebarOpen(false);
                      // Load the conversation's messages from backend (sets messages atomically)
                      loadConversationMessages(conv.id);
                    }}
                    className="w-full px-3 py-2.5 text-left"
                  >
                    <div className="flex items-center gap-2 pr-5">
                      <MessageSquare size={12} className="shrink-0 opacity-40" />
                      <span className="truncate">{conv.title}</span>
                    </div>
                  </button>
                  <button
                    onClick={async (e) => {
                      e.stopPropagation();
                      if (!confirm("Delete this conversation? This cannot be undone.")) return;
                      const token = await getAccessToken();
                      if (!token) return;
                      try {
                        const res = await fetch(`${API_URL}/api/conversations/${conv.id}`, {
                          method: "DELETE",
                          headers: { Authorization: `Bearer ${token}` },
                        });
                        if (res.ok || res.status === 204) {
                          setConversations((prev) => prev.filter((c) => c.id !== conv.id));
                          if (activeConversationId === conv.id) {
                            stopAllConnections();
                            setActiveConversationId(null);
                            setActiveTaskId(null);
                            setMessages([]);
                            setTimelineSteps([]);
                            setPendingPlan(null);
                            setUploadedFiles([]);
                            setUploadSessionUuid(null);
                            setElapsedSeconds(0);
                          }
                          toast.success("Conversation deleted");
                        } else {
                          toast.error("Failed to delete conversation");
                        }
                      } catch {
                        toast.error("Failed to delete conversation");
                      }
                    }}
                    className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 opacity-0 transition-opacity group-hover:opacity-100 hover:bg-red-500/10 hover:text-red-500"
                    title="Delete conversation"
                    aria-label="Delete conversation"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
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
            <div className="flex items-center gap-1">
              <button
                onClick={() => { setMemoryOpen(true); loadMemory(); }}
                className="rounded-md p-1.5 text-muted-foreground/50 hover:text-primary hover:bg-secondary/50 transition-colors"
                title="Memory"
                aria-label="Open memory panel"
              >
                <Brain size={14} />
              </button>
              <button
                onClick={async () => { await logout(); navigate("/"); }}
                className="rounded-md p-1.5 text-muted-foreground/50 hover:text-red-500 hover:bg-secondary/50 transition-colors"
                title="Sign out"
                aria-label="Sign out"
              >
                <LogOut size={14} />
              </button>
            </div>
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

          {/* Running indicator + elapsed timer + graph button */}
          <div className="flex items-center gap-3">
            {/* BUG-018 fix: hide Graph button for quick/instant tier (budget ≤ $0.20) — they have 0 nodes */}
            {activeTaskId && activeInvestigation && activeInvestigation.budget_usd > 0.20 && (
              <Link
                to={`/graph/${activeTaskId}`}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
                title="View Investigation Graph"
                aria-label="View investigation graph"
              >
                <GitBranch size={13} />
                <span className="hidden sm:inline">Graph</span>
              </Link>
            )}
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
          <div className="sr-only" aria-live="polite" aria-atomic="true">
            {latestAnnouncement}
          </div>
          <div className="mx-auto max-w-2xl space-y-4">
            {/* Zero credits banner */}
            {user.tokens <= 0 && (
              <div className="rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-3" role="alert">
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
              <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2" role="status" aria-live="polite">
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

            {/* Loading state when switching conversations */}
            {conversationLoading && messages.length === 0 && (
              <div className="flex items-center justify-center py-24">
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-primary/20 border-t-primary" />
              </div>
            )}

            {/* Empty state */}
            {messages.length === 0 && !isSending && !pendingPlan && !conversationLoading && (
              <div className="flex flex-col items-center justify-center py-24 text-center">
                <h2 className="font-serif text-xl font-semibold text-foreground mb-2">
                  What would you like to know?
                </h2>
                <p className="text-sm text-muted-foreground max-w-md">
                  Ask anything. Mariana adapts — from quick answers to multi-day investigations.
                  Your conversations are saved so you can pick up where you left off.
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
                        // BUG-F2-05: Prefer backend-provided URL (e.g. CDN path) over
                        // constructed API path. Fall back to the constructed path when
                        // the url field is absent or null.
                        const fileUrl = parsed.url
                          ? String(parsed.url)
                          : `${API_URL}/api/investigations/${activeTaskId || ""}/files/${encodeURIComponent(parsed.filename)}`;

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
                                aria-label={`Open attached image ${parsed.filename}`}
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
                            (${Number(parsed.spent_usd).toFixed(2)} incl. fees)
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
                {/* Quality Tier & Loop Controls */}
                <div className="mt-4 flex flex-wrap items-center gap-4">
                  <div className="flex items-center gap-2">
                    <label className="text-[10px] font-medium uppercase tracking-[0.15em] text-muted-foreground">
                      Quality
                    </label>
                    <select
                      value={selectedTier}
                      onChange={(e) => setSelectedTier(e.target.value)}
                      className="rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-foreground outline-none focus:ring-1 focus:ring-accent/50"
                    >
                      <option value="economy">Economy</option>
                      <option value="balanced">Balanced</option>
                      <option value="high">High</option>
                      <option value="maximum">Maximum</option>
                    </select>
                  </div>
                  <label className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={continuousMode}
                      onChange={(e) => setContinuousMode(e.target.checked)}
                      className="h-3.5 w-3.5 rounded border-border text-accent focus:ring-accent/50"
                    />
                    <span className="text-[10px] font-medium uppercase tracking-[0.15em] text-muted-foreground">
                      Continuous Mode
                    </span>
                  </label>
                  {/* BUG-F2-02: dont_kill_branches was never exposed in the UI */}
                  <label className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={dontKillBranches}
                      onChange={(e) => setDontKillBranches(e.target.checked)}
                      className="h-3.5 w-3.5 rounded border-border text-accent focus:ring-accent/50"
                    />
                    <span className="text-[10px] font-medium uppercase tracking-[0.15em] text-muted-foreground">
                      Keep All Branches
                    </span>
                  </label>
                </div>
                {/* BUG-F2-02: user_flow_instructions was never exposed in the UI */}
                <div className="mt-3">
                  <label className="block text-[10px] font-medium uppercase tracking-[0.15em] text-muted-foreground mb-1.5">
                    Flow Instructions <span className="normal-case text-muted-foreground/60">(optional)</span>
                  </label>
                  <textarea
                    value={userFlowInstructions}
                    onChange={(e) => setUserFlowInstructions(e.target.value)}
                    placeholder="Add any custom instructions for how Mariana should conduct this investigation..."
                    rows={2}
                    className="w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-foreground placeholder:text-muted-foreground/40 outline-none focus:ring-1 focus:ring-accent/50 resize-none"
                  />
                </div>
                <div className="mt-4 flex flex-wrap gap-2">
                  <button
                    onClick={handleApprovePlan}
                    // BUG-F3-01: disabled prevents double-approval at the UI layer
                    disabled={isSending || isClassifying}
                    className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-60 disabled:cursor-not-allowed"
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
                <div className="flex items-center gap-3 text-xs">
                  <div className="flex items-center gap-2 text-blue-400">
                    <Loader2 size={12} className="animate-spin" />
                    <span>
                      {timelineSteps.length === 0
                        ? "Mariana is setting up the investigation..."
                        : "Mariana is researching..."}
                    </span>
                  </div>
                  {activeTaskId && (
                    // BUG-F2-03: Added isStopping guard to prevent multiple concurrent
                    // POST /stop requests from rapid clicks. The button is disabled and
                    // shows a spinner while the stop request is in-flight.
                    <button
                      onClick={async () => {
                        if (isStopping) return;
                        const token = await getAccessToken();
                        if (!token || !activeTaskId) return;
                        setIsStopping(true);
                        try {
                          const res = await fetch(`${API_URL}/api/investigations/${activeTaskId}/stop`, {
                            method: "POST",
                            headers: { Authorization: `Bearer ${token}` },
                          });
                          if (!res.ok) {
                            const errText = await res.text().catch(() => res.statusText);
                            throw new Error(`HTTP ${res.status}: ${errText}`);
                          }
                          toast.info("Stop requested", { description: "Investigation will stop after the current cycle." });
                        } catch {
                          toast.error("Failed to stop investigation");
                        } finally {
                          setIsStopping(false);
                        }
                      }}
                      disabled={isStopping}
                      className="inline-flex items-center gap-1 rounded-md border border-red-300/50 px-2.5 py-1 text-[10px] font-medium text-red-400 transition-colors hover:bg-red-500/10 hover:text-red-300 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {isStopping ? (
                        <Loader2 size={10} className="animate-spin" />
                      ) : (
                        <Square size={10} />
                      )}
                      {isStopping ? "Stopping..." : "Stop"}
                    </button>
                  )}
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
                  {activeInvestigation?.output_docx_path && (
                    <button
                      onClick={() => handleDownload("docx")}
                      className="inline-flex items-center gap-1.5 rounded-md border border-green-600/50 px-3 py-1.5 text-xs font-medium text-green-400 hover:bg-green-600/10 transition-colors"
                    >
                      <Download size={12} />
                      Download Word Report
                    </button>
                  )}
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
                aria-label="Ask Mariana a question"
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
          <div
            className="fixed inset-y-0 right-0 z-50 flex w-full max-w-sm flex-col border-l border-border bg-card shadow-2xl animate-slide-in-right"
            role="dialog"
            aria-modal="true"
            aria-labelledby="memory-panel-title"
          >
            <div className="flex items-center justify-between border-b border-border px-4 py-3">
              <div className="flex items-center gap-2">
                <Brain size={16} className="text-primary" />
                <span id="memory-panel-title" className="text-sm font-medium text-foreground">Memory</span>
              </div>
              <button
                onClick={() => setMemoryOpen(false)}
                className="rounded-md p-1.5 text-muted-foreground hover:bg-secondary transition-colors"
                aria-label="Close memory panel"
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
                              aria-label={`Delete stored fact: ${f.fact}`}
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
                              aria-label={`Delete preference ${key}`}
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
