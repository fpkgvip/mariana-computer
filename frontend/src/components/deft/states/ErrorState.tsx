/**
 * P13 — Calm error surface for any list / async failure.
 *
 * Pattern: a single amber-toned card with a one-line cause, a Try again
 * button (when retryable), and a copyable request id when present. The
 * surface is intentionally quiet — no exclamation, no destructive red unless
 * the failure is actually destructive (delete, charge). For routine load
 * failures the muted palette keeps the rest of the page legible.
 *
 * Props:
 *  - title:        short label, default "Could not load this"
 *  - message:      one-line cause from the backend / network
 *  - requestId:    optional request id surfaced from ApiError
 *  - onRetry:      if provided, renders a Try again button
 *  - retrying:     when true, the retry button enters a pending state
 *  - tone:         "muted" (default — for non-destructive load failures)
 *                  "destructive" (for failed mutations: delete, charge, etc.)
 *  - dense:        compact variant for sidebars / small panels
 */
import { AlertCircle, RotateCcw, Copy, Check } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";

export interface ErrorStateProps {
  title?: string;
  message?: string | null;
  requestId?: string | null;
  onRetry?: () => void;
  retrying?: boolean;
  tone?: "muted" | "destructive";
  dense?: boolean;
  /** Optional: extra action shown next to Try again (e.g. "Report issue"). */
  secondary?: React.ReactNode;
}

export function ErrorState({
  title = "Could not load this",
  message,
  requestId,
  onRetry,
  retrying = false,
  tone = "muted",
  dense = false,
  secondary,
}: ErrorStateProps) {
  const [copied, setCopied] = useState(false);

  const handleCopyId = async () => {
    if (!requestId) return;
    try {
      await navigator.clipboard.writeText(requestId);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // ignore — clipboard not available
    }
  };

  const isDestructive = tone === "destructive";

  return (
    <div
      role="alert"
      aria-live="polite"
      className={cn(
        "rounded-md border",
        dense ? "px-3 py-2.5" : "px-4 py-3.5",
        isDestructive
          ? "border-destructive/40 bg-destructive/5 text-destructive"
          : "border-border/70 bg-surface-1/40 text-foreground",
      )}
    >
      <div className="flex items-start gap-2.5">
        <AlertCircle
          size={dense ? 12 : 14}
          aria-hidden
          className={cn(
            "mt-0.5 shrink-0",
            isDestructive ? "text-destructive" : "text-muted-foreground",
          )}
        />
        <div className="min-w-0 flex-1">
          <p className={cn("font-medium leading-tight", dense ? "text-xs" : "text-sm")}>
            {title}
          </p>
          {message ? (
            <p
              className={cn(
                "mt-1 leading-relaxed",
                dense ? "text-[11px]" : "text-xs",
                isDestructive ? "text-destructive/90" : "text-muted-foreground",
              )}
            >
              {message}
            </p>
          ) : null}
          {(onRetry || secondary || requestId) && (
            <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
              {onRetry && (
                <button
                  type="button"
                  onClick={onRetry}
                  disabled={retrying}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11px] font-medium transition-colors",
                    "disabled:cursor-not-allowed disabled:opacity-60",
                    isDestructive
                      ? "border-destructive/40 text-destructive hover:bg-destructive/10"
                      : "border-border bg-background text-foreground hover:bg-secondary",
                  )}
                >
                  <RotateCcw
                    size={11}
                    aria-hidden
                    className={cn(retrying && "animate-spin")}
                  />
                  {retrying ? "Retrying" : "Try again"}
                </button>
              )}
              {secondary}
              {requestId ? (
                <button
                  type="button"
                  onClick={handleCopyId}
                  aria-label="Copy request id"
                  className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
                  title={`Request id: ${requestId}`}
                >
                  {copied ? <Check size={10} aria-hidden /> : <Copy size={10} aria-hidden />}
                  <span>req {shorten(requestId)}</span>
                </button>
              ) : null}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function shorten(id: string): string {
  if (id.length <= 8) return id;
  return id.slice(0, 8);
}
