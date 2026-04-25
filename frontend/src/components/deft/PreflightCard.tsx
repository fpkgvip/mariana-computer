/**
 * F2 — Pre-flight Card
 *
 * Sits below the Prompt Bar after the user types a prompt.
 *
 * Renders the receipt promise: estimated credit range, estimated duration,
 * tier picker (lite/standard/max), credit ceiling slider. The user must click
 * "Start" to actually launch the run. The quote is advisory; the ceiling is a
 * contract enforced server-side.
 *
 * Behaviors:
 *  - Debounces /api/agent/quote 350ms after the user stops typing
 *  - Cancels in-flight quote requests when the prompt changes
 *  - Shows skeleton + spinner while fetching
 *  - Surfaces 402-class errors clearly (insufficient balance)
 *  - Disables Start when the user has no balance OR ceiling > balance
 */

import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  Clock,
  Coins,
  Loader2,
  Play,
  ShieldCheck,
  Sparkles,
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
  className?: string;
}

const QUOTE_DEBOUNCE_MS = 350;

export function PreflightCard({
  prompt,
  onStart,
  balance,
  defaultTier = "standard",
  starting = false,
  className,
}: PreflightCardProps) {
  const [tier, setTier] = useState<ModelTier>(defaultTier);
  const [ceiling, setCeiling] = useState<number>(0);
  const [ceilingTouched, setCeilingTouched] = useState(false);
  const [quote, setQuote] = useState<QuoteResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

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

  const insufficient = quote ? balance < quote.credits_min : false;
  const ceilingBelowMin = quote ? ceiling < quote.credits_min : false;
  const ceilingDollars = (ceiling / 100).toFixed(2);

  const ceilingMax = quote ? Math.max(quote.credits_max * 2, balance) : Math.max(100, balance);
  const ceilingMin = quote ? Math.max(1, Math.floor(quote.credits_min * 0.5)) : 1;
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
          <ShieldCheck size={14} className="text-accent" aria-hidden />
          Pre-flight
        </div>
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
          Promise a ceiling, deliver a receipt.
        </span>
      </div>

      {/* Quote line */}
      <div className="mt-3 flex flex-wrap items-baseline gap-x-4 gap-y-1">
        {loading && !quote ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 size={14} className="animate-spin" aria-hidden />
            Estimating…
          </div>
        ) : error ? (
          <div className="flex items-center gap-2 text-sm text-destructive">
            <AlertCircle size={14} aria-hidden />
            {error}
          </div>
        ) : quote ? (
          <>
            <div className="flex items-center gap-1.5 text-base font-semibold text-foreground">
              <Coins size={14} className="text-accent" aria-hidden />
              {formatCreditsRange(quote.credits_min, quote.credits_max)}
              <span className="text-sm font-normal text-muted-foreground">
                ({formatDollarsRange(quote.credits_min, quote.credits_max)})
              </span>
            </div>
            <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
              <Clock size={12} aria-hidden />
              {formatEtaRange(quote.eta_seconds_min, quote.eta_seconds_max)}
            </div>
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Sparkles size={11} aria-hidden />
              complexity {quote.complexity_score.toFixed(2)}
            </div>
          </>
        ) : null}
      </div>

      {/* Tier selector */}
      <fieldset className="mt-4">
        <legend className="mb-1.5 text-xs uppercase tracking-wide text-muted-foreground">
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
              className={cn(
                "flex flex-col items-start rounded-lg border px-3 py-2 text-left transition-colors duration-150",
                tier === t
                  ? "border-accent bg-[hsl(var(--accent-muted))] text-foreground"
                  : "border-border bg-secondary text-muted-foreground hover:border-[hsl(var(--bg-4))] hover:text-foreground",
              )}
            >
              <span className="text-sm font-medium">{TIER_LABEL[t]}</span>
              <span className="text-[11px] leading-snug">{TIER_DESCRIPTION[t]}</span>
            </button>
          ))}
        </div>
      </fieldset>

      {/* Ceiling slider */}
      <div className="mt-4">
        <div className="mb-1.5 flex items-baseline justify-between text-xs">
          <span className="uppercase tracking-wide text-muted-foreground">
            Credit ceiling
          </span>
          <span className="font-mono text-sm text-foreground">
            {ceiling.toLocaleString()} <span className="text-muted-foreground">credits · ${ceilingDollars}</span>
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
        <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
          <span>{ceilingMin.toLocaleString()}</span>
          <span>{effectiveCeilingMax.toLocaleString()}</span>
        </div>
      </div>

      {/* Footer: balance + Start */}
      <div className="mt-4 flex items-center justify-between gap-3">
        <div className="text-xs text-muted-foreground">
          Balance:{" "}
          <span className={cn("font-mono", insufficient && "text-destructive")}>
            {balance.toLocaleString()} credits
          </span>
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
            canStart
              ? "bg-accent text-accent-foreground hover:opacity-90 active:scale-[0.98]"
              : "cursor-not-allowed bg-secondary text-muted-foreground",
          )}
        >
          {starting ? (
            <Loader2 size={14} className="animate-spin" aria-hidden />
          ) : (
            <Play size={14} aria-hidden />
          )}
          Start
        </button>
      </div>

      {(insufficient || ceilingBelowMin) && quote && (
        <div
          role="alert"
          className="mt-3 flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        >
          <AlertCircle size={12} className="mt-0.5 shrink-0" aria-hidden />
          {insufficient
            ? `Estimated minimum ${quote.credits_min} credits exceeds your balance.`
            : `Ceiling is below the estimated minimum of ${quote.credits_min} credits.`}{" "}
          <a className="ml-1 font-medium underline" href="/checkout">
            Top up
          </a>
        </div>
      )}
    </div>
  );
}
