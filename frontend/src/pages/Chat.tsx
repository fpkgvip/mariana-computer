import { useState, useEffect, useRef, useCallback } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Link, useNavigate } from "react-router-dom";
import { Send, AlertTriangle, ChevronDown, Menu, X } from "lucide-react";
import { toast } from "sonner";
import { supabase } from "@/lib/supabase";

interface Message {
  role: "user" | "assistant" | "system";
  content: string;
  type?: "text" | "code" | "status";
}

/** Shape of an investigation returned by the backend */
interface InvestigationResponse {
  id: string;
  status: "PENDING" | "RUNNING" | "COMPLETED" | "FAILED";
  ticker?: string;
  hypothesis?: string;
  findings?: string;
  status_message?: string;
  error?: string;
}

const API_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://77.42.3.206:8080";

const depthOptions = [
  { value: "deep", label: "Deep Research", time: "2–5 min", warn: false },
  { value: "superdeep", label: "Superdeep", time: "15+ min", warn: false },
  { value: "professional", label: "Professional", time: "1–2+ hrs", warn: false },
  { value: "flagship", label: "Mariana Flagship", time: "12+ hrs", warn: true },
];

const modelOptions = [
  { value: "cheap", label: "Cheap", warn: false },
  { value: "fast", label: "Fast", warn: false },
  { value: "pro", label: "Pro", warn: false },
  { value: "frontier", label: "Frontier", warn: true },
];

const demoMessages: Message[] = [
  {
    role: "user",
    content:
      "Investigate whether Acme Corp's reported gross margins are consistent with their disclosed supplier relationships. Check for any undisclosed related-party transactions in their last 8 quarterly filings.",
  },
  {
    role: "system",
    content: "Starting investigation. Pulling 10-Q filings from SEC EDGAR...",
    type: "status",
  },
  {
    role: "assistant",
    content:
      "I've parsed Acme Corp's 10-Q filings from Q1 2024 through Q4 2025. Their reported gross margin averaged 34.2%, but I'm seeing some inconsistencies in COGS line items that warrant deeper examination.\n\nI'm now cross-referencing their disclosed suppliers against corporate registry data to map the full supplier network.",
    type: "text",
  },
  {
    role: "system",
    content:
      "Writing custom scraper for trade registry cross-referencing...\nQuerying corporate ownership databases across 3 jurisdictions...",
    type: "status",
  },
  {
    role: "assistant",
    content:
      "I've identified a pattern. One of Acme's top-3 suppliers by spend shares a registered director with an Acme subsidiary incorporated in Delaware. This relationship isn't disclosed in the related-party footnotes.\n\nI'm building a regression model to test whether procurement pricing from this supplier deviates from market rates.",
    type: "text",
  },
  {
    role: "system",
    content:
      "Built pricing regression model (OLS, 340 data points)\nR² = 0.87 — supplier pricing is 12-18% above comparable market rates\nGenerating report with full methodology and exhibits...",
    type: "status",
  },
];

/**
 * Extract a ticker symbol from the user's message.
 * Looks for patterns like "$AAPL", "AAPL:", or bare uppercase words
 * 2–5 characters long that look like tickers. Falls back to "GENERAL".
 */
function extractTicker(message: string): string {
  // "$TICKER" pattern
  const dollarMatch = message.match(/\$([A-Z]{1,5})\b/);
  if (dollarMatch) return dollarMatch[1];

  // "TICKER:" or "TICKER " at start
  const upperMatch = message.match(/\b([A-Z]{2,5})\b/);
  if (upperMatch && upperMatch[1] !== "I" && upperMatch[1] !== "A") {
    return upperMatch[1];
  }

  return "GENERAL";
}

/** Retrieve the current Supabase access token, or null if not authenticated */
async function getAccessToken(): Promise<string | null> {
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

export default function Chat() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [showDemo, setShowDemo] = useState(true);
  const [depth, setDepth] = useState("deep");
  const [model, setModel] = useState("fast");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [isSending, setIsSending] = useState(false);

  // Track the currently running investigation id for polling
  const currentInvestigationId = useRef<string | null>(null);
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!user) navigate("/login");
  }, [user, navigate]);

  // Animate demo messages on first load
  useEffect(() => {
    if (!showDemo) return;
    let i = 0;
    const interval = setInterval(() => {
      const msg = demoMessages[i];
      if (i < demoMessages.length && msg) {
        setMessages((prev) => [...prev, msg]);
        i++;
      } else {
        clearInterval(interval);
      }
    }, 800);
    return () => clearInterval(interval);
  }, [showDemo]);

  // Clean up polling on unmount
  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    };
  }, []);

  /** Stop the active polling interval */
  const stopPolling = useCallback(() => {
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
    currentInvestigationId.current = null;
    setIsSending(false);
  }, []);

  /**
   * Poll GET /api/investigations/{id} every 5 seconds.
   * Appends status updates and findings as messages.
   * Stops when status is COMPLETED or FAILED.
   */
  const startPolling = useCallback(
    (investigationId: string, token: string) => {
      currentInvestigationId.current = investigationId;

      const poll = async () => {
        try {
          const res = await fetch(
            `${API_URL}/api/investigations/${investigationId}`,
            {
              headers: {
                Authorization: `Bearer ${token}`,
                "Content-Type": "application/json",
              },
            }
          );

          if (!res.ok) {
            console.error("[Chat] Poll failed:", res.status, res.statusText);
            return;
          }

          const data: InvestigationResponse = await res.json();

          // Append a status update if there is one
          if (data.status_message) {
            setMessages((prev) => [
              ...prev,
              {
                role: "system" as const,
                content: data.status_message!,
                type: "status" as const,
              },
            ]);
          }

          // Terminal states
          if (data.status === "COMPLETED") {
            if (data.findings) {
              setMessages((prev) => [
                ...prev,
                {
                  role: "assistant" as const,
                  content: data.findings!,
                  type: "text" as const,
                },
              ]);
            }
            stopPolling();
          } else if (data.status === "FAILED") {
            const errMsg =
              data.error ?? "The investigation failed. Please try again.";
            setMessages((prev) => [
              ...prev,
              {
                role: "assistant" as const,
                content: errMsg,
                type: "text" as const,
              },
            ]);
            toast.error("Investigation failed", { description: errMsg });
            stopPolling();
          }
        } catch (err) {
          console.error("[Chat] Polling error:", err);
        }
      };

      // Poll immediately, then every 5 seconds
      poll();
      pollIntervalRef.current = setInterval(poll, 5000);
    },
    [stopPolling]
  );

  const handleSend = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isSending) return;

    // If demo is showing, clear it and start fresh
    if (showDemo) {
      setShowDemo(false);
      setMessages([]);
    }

    const userMessage = input.trim();
    setInput("");
    setIsSending(true);

    // Add the user message immediately
    setMessages((prev) => [
      ...prev,
      { role: "user", content: userMessage, type: "text" },
    ]);

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
    setMessages((prev) => [
      ...prev,
      {
        role: "system",
        content: "Initializing research environment...",
        type: "status",
      },
    ]);

    // POST to create investigation
    try {
      const ticker = extractTicker(userMessage);
      const res = await fetch(`${API_URL}/api/investigations`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          ticker,
          hypothesis: userMessage,
          budget_usd: 50.0,
          depth,
          model,
        }),
      });

      if (!res.ok) {
        const errorText = await res.text().catch(() => res.statusText);
        throw new Error(`HTTP ${res.status}: ${errorText}`);
      }

      const data: InvestigationResponse = await res.json();

      setMessages((prev) => [
        ...prev,
        {
          role: "system",
          content: `Investigation started (ID: ${data.id}). Monitoring progress...`,
          type: "status",
        },
      ]);

      // Begin polling for updates
      startPolling(data.id, token);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Unknown error";
      toast.error("Failed to start investigation", { description: message });
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: `Could not start the investigation: ${message}. Please try again.`,
          type: "text",
        },
      ]);
      setIsSending(false);
    }
  };

  const selectedDepth = depthOptions.find((d) => d.value === depth);
  const selectedModel = modelOptions.find((m) => m.value === model);
  const showHighCostWarning = selectedDepth?.warn || selectedModel?.warn;

  if (!user) return null;

  return (
    <div className="flex h-screen bg-background">
      {/* Mobile sidebar overlay */}
      {sidebarOpen && (
        <div className="fixed inset-0 z-40 bg-black/40 md:hidden" onClick={() => setSidebarOpen(false)} />
      )}

      {/* Sidebar */}
      <div
        className={`fixed inset-y-0 left-0 z-50 w-64 flex-col border-r border-border bg-card transition-transform duration-300 md:relative md:z-auto md:flex md:translate-x-0 ${
          sidebarOpen ? "flex translate-x-0" : "hidden -translate-x-full"
        }`}
      >
        <div className="flex h-16 items-center justify-between border-b border-border px-5">
          <Link to="/" className="font-serif text-sm font-semibold text-foreground">
            Mariana
          </Link>
          <button onClick={() => setSidebarOpen(false)} className="md:hidden text-muted-foreground">
            <X size={18} />
          </button>
        </div>
        <div className="flex-1 px-4 py-4">
          <p className="mb-2 text-[10px] font-medium uppercase tracking-[0.15em] text-muted-foreground">
            Active sessions
          </p>
          <div className="space-y-1">
            <div className="rounded-md bg-secondary px-3 py-2 text-xs text-foreground">
              Acme Corp investigation
            </div>
          </div>
        </div>
        <div className="border-t border-border px-4 py-3">
          <div className="text-xs text-muted-foreground">
            <span className="font-medium text-foreground">${(user.tokens / 10).toFixed(2)}</span>{" "}
            credit remaining
          </div>
          <p className="mt-1 text-[10px] text-muted-foreground">
            {user.name} · {user.email}
          </p>
        </div>
      </div>

      {/* Main */}
      <div className="flex flex-1 flex-col">
        <div className="flex h-16 items-center justify-between border-b border-border px-4 sm:px-6">
          <div className="flex items-center gap-3">
            <button onClick={() => setSidebarOpen(true)} className="md:hidden text-foreground" aria-label="Open sidebar">
              <Menu size={20} />
            </button>
            <Link to="/" className="font-serif text-sm font-semibold text-foreground md:hidden">
              Mariana
            </Link>
            <span className="hidden text-xs text-muted-foreground md:inline">
              Mariana Computer
            </span>
          </div>
          <div className="text-xs text-muted-foreground md:hidden">
            ${(user.tokens / 10).toFixed(2)} credit
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-6 sm:px-6">
          <div className="mx-auto max-w-2xl space-y-4">
            {messages.filter(Boolean).map((msg, i) => (
              <div key={i} className="animate-fade-in" style={{ animationDelay: `${i * 50}ms` }}>
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
                ) : (
                  <div className="max-w-[90%] text-sm leading-relaxed text-muted-foreground sm:max-w-lg">
                    {msg.content}
                  </div>
                )}
              </div>
            ))}

            {messages.length > 0 && showDemo && messages.length >= demoMessages.length && (
              <div className="mt-6 rounded-lg bg-secondary/50 px-4 py-3 text-xs text-muted-foreground ring-1 ring-border">
                <p className="text-foreground font-medium">This is a demo.</p>
                <p className="mt-1">
                  In production, Mariana runs autonomously — you can close this
                  tab and you'll be notified when the investigation is complete.
                </p>
              </div>
            )}
          </div>
        </div>

        {/* Input area */}
        <div className="border-t border-border px-4 py-4 sm:px-6">
          <div className="mx-auto max-w-2xl">
            {/* Selectors */}
            <div className="mb-3 flex flex-wrap items-center gap-2 sm:gap-3">
              <div className="relative">
                <select
                  value={depth}
                  onChange={(e) => setDepth(e.target.value)}
                  className="appearance-none rounded-md border border-border bg-card py-1.5 pl-3 pr-8 text-xs text-foreground focus:border-primary focus:outline-none"
                  disabled={isSending}
                >
                  {depthOptions.map((d) => (
                    <option key={d.value} value={d.value}>
                      {d.label} ({d.time})
                    </option>
                  ))}
                </select>
                <ChevronDown size={12} className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
              </div>

              <div className="relative">
                <select
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  className="appearance-none rounded-md border border-border bg-card py-1.5 pl-3 pr-8 text-xs text-foreground focus:border-primary focus:outline-none"
                  disabled={isSending}
                >
                  {modelOptions.map((m) => (
                    <option key={m.value} value={m.value}>
                      Model: {m.label}
                    </option>
                  ))}
                </select>
                <ChevronDown size={12} className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
              </div>
            </div>

            {/* Warning */}
            {showHighCostWarning && (
              <div className="mb-3 flex items-start gap-2 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800 ring-1 ring-amber-200">
                <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                <span>
                  {selectedDepth?.warn && selectedModel?.warn
                    ? "Flagship research with Frontier models can consume a significant number of tokens. Review the cost estimate carefully before proceeding."
                    : selectedDepth?.warn
                    ? "Mariana Flagship research runs for 12+ hours and can consume a significant number of tokens."
                    : "Frontier models use significantly more tokens per query than other tiers."}
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
