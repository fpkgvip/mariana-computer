/**
 * F2 — Pre-flight Card
 *
 * Sits below the Prompt Bar after the user types a prompt.
 *
 * Renders the receipt promise: estimated credit range, estimated duration,
 * tier picker (Lite/Standard/Max with trade-off tooltip), credit ceiling
 * with discrete stops + numeric twin. The user must click "Start" to
 * launch the run. The quote is advisory; the ceiling is a contract enforced
 * server-side.
 *
 * Behaviors:
 *  - Debounces /api/agent/quote 350ms after the user stops typing
 *  - Cancels in-flight quote requests when the prompt changes
 *  - Shows skeleton + spinner while fetching
 *  - Surfaces 402-class errors clearly with calm copy + Add-credits link
 *  - Disables Start when the user has no balance OR ceiling > balance
 *  - Admins (`unlimited`) skip the ceiling row entirely
 */

import { useEffect, useId, useRef, useState } from "react";
import {
  AlertCircle,
  Clock,
  Coins,
  Loader2,
  Play,
  ShieldCheck,
} from "lucide-react";
import {
  fetchQuote,
  formatCreditsRange,
  formatDollarsRange,
  formatEtaRange,
  type ModelTier,
  type QuoteResponse,
  TIER_DESCRIPTION,
  TIER_LABEL,
  TIER_TRADEOFF,
} from "@/lib/agentApi";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import { track } from "@/lib/analytics";

interface PreflightCardProps {
  prompt: string;
  /** Called with chosen tier + ceiling when the user clicks Start. */
  onStart: (params: { tier: ModelTier; ceiling: number; quote: QuoteResponse }) => void | Promise<void>;
  /** User's current credit balance for the ceiling slider + warning. */
  balance: number;
  /** Optional default tier override (e.g. inferred from slash command). */
  defaultTier?: ModelTier;
  /** Show inline busy spinner on Start (e.g. while POST /api/agent/run dispatches). */
  starting?: boolean;
  /** Admins are billed internally; bypass balance/ceiling guards and hide the row. */
  unlimited?: boolean;
  className?: string;
}

const QUOTE_DEBOUNCE_MS = 350;

/** Discrete ceiling stops shown as chips. UNLIMITED renders for admins only (hidden in unlimited mode). */
const CEILING_STOPS: Array<{ label: string; value: number }> = [
  { label: "100", value: 100 },
  { label: "250", value: 250 },
  { label: "500", value: 500 },
  { label: "1,000", value: 1_000 },
];

export function PreflightCard({
  prompt,
  onStart,
  balance,
  defaultTier = "standard",
  starting = false,
  unlimited = false,
  className,
}: PreflightCardProps) {
  const [tier, setTier] = useState<ModelTier>(defaultTier);
  const [ceiling, setCeiling] = useState<number>(0);
  const [ceilingTouched, setCeilingTouched] = useState(false);
  const [quote, setQuote] = useState<QuoteResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const ceilingInputId = useId();

  const trimmed = prompt.trim();

  // Debounced quote fetch
  useEffect(() => {
    if (!trimmed) {
      setQuote(null);
      setError(null);
      setLoading(false);
      abortRef.current?.abort();
      return;
    }
    setLoading(true);
    setError(null);
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;

    const handle = window.setTimeout(async () => {
      try {
        const q = await fetchQuote({ prompt: trimmed, tier }, controller.signal);
        setQuote(q);
        try {
          track("quote_generated", {
            tier,
            credits_min: q.credits_min,
            credits_max: q.credits_max,
          });
        } catch {
          // ignore
        }
        // If user hasn't touched the ceiling, default to the quote max (capped at balance).
        if (!ceilingTouched) {
          setCeiling(Math.min(q.credits_max, balance > 0 ? balance : q.credits_max));
        }
        setError(null);
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError("Could not fetch a quote. Try again in a moment.");
        }
        setQuote(null);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }, QUOTE_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(handle);
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- ceiling intentionally excluded
  }, [trimmed, tier, balance]);

  if (!trimmed) return null;

  const insufficient = unlimited ? false : quote ? balance < quote.credits_min : false;
  const ceilingBelowMin = quote && !unlimited ? ceiling < quote.credits_min : false;
  const ceilingDollars = (ceiling / 100).toFixed(2);

  const ceilingFloor = quote ? Math.max(quote.credits_max * 2, 100) : 100;
  const ceilingMin = quote ? Math.max(1, Math.floor(quote.credits_min * 0.5)) : 1;
  const ceilingMax = quote
    ? Math.max(ceilingFloor, balance > 0 ? balance : ceilingFloor)
    : Math.max(100, balance);
  const effectiveCeilingMax = Math.max(ceilingMin, ceilingMax);

  const canStart = Boolean(quote) && !insufficient && !ceilingBelowMin && !starting && !loading;

  return (
    <div
      className={cn(
        "rounded-xl border border-border bg-card p-4 shadow-sm",
        "animate-in fade-in slide-in-from-bottom-1 duration-150",
        className,
      )}
      role="region"
      aria-label="Pre-flight quote"
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-medium text-foreground">
          <ShieldCheck size={14} className="text-accent" aria-hidden="true" />
          Pre-flight
        </div>
        <span className="text-[11px] tracking-wide text-muted-foreground">
          {unlimited ? "Internal account" : "Set a ceiling. Pay only for what runs."}
        </span>
      </div>

      {/* Quote line + provenance */}
      <div className="mt-3 space-y-1">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          {loading && !quote ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 size={14} className="animate-spin" aria-hidden="true" />
              Estimating…
            </div>
          ) : error ? (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertCircle size={14} aria-hidden="true" />
              {error}
            </div>
          ) : quote ? (
            <>
              <div className="flex items-center gap-1.5 text-base font-semibold text-foreground">
                <Coins size={14} className="text-accent" aria-hidden="true" />
                {formatCreditsRange(quote.credits_min, quote.credits_max)}
                <span className="text-sm font-normal text-muted-foreground">
                  ({formatDollarsRange(quote.credits_min, quote.credits_max)})
                </span>
              </div>
              <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
                <Clock size={12} aria-hidden="true" />
                {formatEtaRange(quote.eta_seconds_min, quote.eta_seconds_max)}
              </div>
            </>
          ) : null}
        </div>
        {quote && !loading && !error && (
          <p className="text-[11px] text-muted-foreground">
            Based on your last 50 runs.
          </p>
        )}
      </div>

      {/* Tier selector */}
      <fieldset className="mt-4">
        <legend className="mb-1.5 text-xs tracking-wide text-muted-foreground">
          Model tier
        </legend>
        <div className="grid grid-cols-3 gap-2" role="radiogroup">
          {(["lite", "standard", "max"] as ModelTier[]).map((t) => (
            <button
              key={t}
              type="button"
              role="radio"
              aria-checked={tier === t}
              onClick={() => setTier(t)}
              title={TIER_TRADEOFF[t]}
              className={cn(
                "group relative flex flex-col items-start rounded-lg border px-3 py-2 text-left transition-colors duration-150",
                "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                tier === t
                  ? "border-accent bg-[hsl(var(--accent-muted))] text-foreground"
                  : "border-border bg-secondary text-muted-foreground hover:border-[hsl(var(--bg-4))] hover:text-foreground",
              )}
            >
              <span className="text-sm font-medium">{TIER_LABEL[t]}</span>
              <span className="text-[11px] leading-snug">{TIER_DESCRIPTION[t]}</span>
              {/* Hover trade-off tooltip — visible on hover/focus */}
              <span
                role="tooltip"
                className={cn(
                  "pointer-events-none absolute left-0 right-0 top-full z-20 mt-1.5 rounded-md border border-border bg-popover px-2.5 py-1.5 text-[11px] leading-snug text-foreground shadow-md",
                  "opacity-0 translate-y-1 transition-all duration-150",
                  "group-hover:opacity-100 group-hover:translate-y-0",
                  "group-focus-visible:opacity-100 group-focus-visible:translate-y-0",
                )}
              >
                {TIER_TRADEOFF[t]}
              </span>
            </button>
          ))}
        </div>
      </fieldset>

      {/* Ceiling — hidden for unlimited (admin) accounts */}
      {!unlimited && (
        <div className="mt-4">
          <div className="mb-1.5 flex items-baseline justify-between text-xs">
            <label htmlFor={ceilingInputId} className="tracking-wide text-muted-foreground">
              Credit ceiling
            </label>
            <span className="font-mono text-sm text-foreground">
              <input
                id={ceilingInputId}
                type="number"
                min={ceilingMin}
                max={effectiveCeilingMax}
                step={1}
                value={Math.max(ceilingMin, Math.min(ceiling, effectiveCeilingMax))}
                onChange={(e) => {
                  const n = Number(e.target.value);
                  if (Number.isFinite(n)) {
                    setCeiling(n);
                    setCeilingTouched(true);
                  }
                }}
                aria-label="Credit ceiling (numeric)"
                className={cn(
                  "w-20 rounded border border-border bg-secondary px-1.5 py-0.5 text-right text-sm text-foreground",
                  "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                )}
              />{" "}
              <span className="text-muted-foreground">credits · ${ceilingDollars}</span>
            </span>
          </div>
          <input
            type="range"
            min={ceilingMin}
            max={effectiveCeilingMax}
            step={1}
            value={Math.max(ceilingMin, Math.min(ceiling, effectiveCeilingMax))}
            onChange={(e) => {
              setCeiling(Number(e.target.value));
              setCeilingTouched(true);
            }}
            className={cn(
              "h-1.5 w-full appearance-none rounded-full bg-secondary outline-none",
              "[&::-webkit-slider-thumb]:appearance-none",
              "[&::-webkit-slider-thumb]:h-4",
              "[&::-webkit-slider-thumb]:w-4",
              "[&::-webkit-slider-thumb]:rounded-full",
              "[&::-webkit-slider-thumb]:bg-accent",
              "[&::-webkit-slider-thumb]:cursor-pointer",
              "[&::-moz-range-thumb]:h-4",
              "[&::-moz-range-thumb]:w-4",
              "[&::-moz-range-thumb]:rounded-full",
              "[&::-moz-range-thumb]:bg-accent",
              "[&::-moz-range-thumb]:border-0",
              "[&::-moz-range-thumb]:cursor-pointer",
            )}
            aria-label="Credit ceiling"
            aria-valuemin={ceilingMin}
            aria-valuemax={effectiveCeilingMax}
            aria-valuenow={ceiling}
          />
          {/* Discrete stops */}
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {CEILING_STOPS.map((s) => {
              const active = ceiling === s.value;
              const reachable = s.value <= effectiveCeilingMax;
              return (
                <button
                  key={s.value}
                  type="button"
                  onClick={() => {
                    setCeiling(s.value);
                    setCeilingTouched(true);
                  }}
                  disabled={!reachable}
                  className={cn(
                    "rounded-md border px-2 py-0.5 text-[11px] transition-colors",
                    "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                    active
                      ? "border-accent bg-[hsl(var(--accent-muted))] text-foreground"
                      : "border-border bg-secondary text-muted-foreground hover:text-foreground",
                    !reachable && "cursor-not-allowed opacity-40",
                  )}
                >
                  {s.label}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Footer: balance + Start */}
      <div className="mt-4 flex items-center justify-between gap-3">
        <div className="text-xs text-muted-foreground">
          {unlimited ? (
            <span className="font-mono text-foreground">Internal account · usage not charged</span>
          ) : (
            <>
              Balance:{" "}
              <span className={cn("font-mono", insufficient && "text-destructive")}>
                {balance.toLocaleString()} credits
              </span>
            </>
          )}
        </div>
        <button
          type="button"
          onClick={() => {
            if (canStart && quote) onStart({ tier, ceiling, quote });
          }}
          disabled={!canStart}
          aria-label="Start run"
          className={cn(
            "inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-150",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
            canStart
              ? "bg-accent text-accent-foreground hover:opacity-90 active:scale-[0.98]"
              : "cursor-not-allowed bg-secondary text-muted-foreground",
          )}
        >
          {starting ? (
            <Loader2 size={14} className="animate-spin" aria-hidden="true" />
          ) : (
            <Play size={14} aria-hidden="true" />
          )}
          Start
        </button>
      </div>

      {(insufficient || ceilingBelowMin) && quote && (
        <div
          role="alert"
          className="mt-3 flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        >
          <AlertCircle size={12} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>
            {insufficient
              ? `Not enough credits. This run needs at least ${quote.credits_min.toLocaleString()} to start.`
              : `Raise the ceiling to at least ${quote.credits_min.toLocaleString()} credits to start.`}
            {insufficient && (
              <>
                {" "}
                <a className="font-medium underline underline-offset-2" href="/checkout">
                  Add credits
                </a>
              </>
            )}
          </span>
        </div>
      )}
    </div>
  );
}
