/**
 * 4-step onboarding wizard shown to first-time users.
 *
 *   1. Name        – capture full_name (skip allowed; stored in profiles)
 *   2. Vault       – brief explainer + opt-in to /vault setup (skip allowed)
 *   3. First prompt – pick a suggested prompt that prefills /build on finish
 *   4. Quote demo  – live POST /api/agent/quote, show range + ETA
 *
 * State is local + persisted in localStorage under DEFT_ONBOARDING_KEY.
 * "Skip everything" sets the seen flag and dismisses the dialog.
 *
 * Accessibility:
 *  - Each step uses role="dialog" (provided by shadcn DialogContent),
 *    aria-labelledby, and a single visible focus per step.
 *  - All icons in interactive controls have aria-hidden + accessible label.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ArrowRight, Check, Loader2, KeyRound, Sparkles } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { supabase } from "@/lib/supabase";
import {
  fetchQuote,
  formatCreditsRange,
  formatDollarsRange,
  formatEtaRange,
  type QuoteResponse,
} from "@/lib/agentApi";
import { track } from "@/lib/analytics";

const STORAGE_KEY = "deft.onboarding.v1";

interface StoredState {
  completed?: boolean;
  skipped?: boolean;
  step?: number;
  full_name?: string;
  selected_prompt?: string;
}

function readStored(): StoredState {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as StoredState) : {};
  } catch {
    return {};
  }
}

function writeStored(next: StoredState): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    /* storage may be disabled — degrade silently */
  }
}

const SUGGESTED_PROMPTS = [
  "Build me a Pomodoro timer web app with a clean glass-morphism UI.",
  "Research the top 5 vector databases and write a comparison report (PDF).",
  "Take this CSV of sales data and build me an interactive dashboard.",
];

interface Props {
  /** When true, renders even if the user has already completed/skipped. */
  forceOpen?: boolean;
  /** External controlled open state, bypassing storage gating. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

export default function OnboardingWizard({
  forceOpen = false,
  open: controlledOpen,
  onOpenChange,
}: Props) {
  const { user } = useAuth();
  const navigate = useNavigate();

  const [internalOpen, setInternalOpen] = useState<boolean>(false);
  const [step, setStep] = useState<number>(1);
  const [fullName, setFullName] = useState<string>("");
  const [selectedPrompt, setSelectedPrompt] = useState<string>(
    SUGGESTED_PROMPTS[0],
  );
  const [quote, setQuote] = useState<QuoteResponse | null>(null);
  const [quoteLoading, setQuoteLoading] = useState<boolean>(false);
  const [quoteError, setQuoteError] = useState<string | null>(null);
  const [savingName, setSavingName] = useState<boolean>(false);
  const lastTrackedStep = useRef<number | null>(null);

  // Resolve open state: explicit-controlled wins; otherwise gate by storage.
  const open = controlledOpen ?? internalOpen;

  // First-load gating
  useEffect(() => {
    if (controlledOpen !== undefined) return; // controlled
    if (!user) return;
    const stored = readStored();
    if (forceOpen) {
      setInternalOpen(true);
      return;
    }
    if (!stored.completed && !stored.skipped) {
      setInternalOpen(true);
      if (typeof stored.step === "number" && stored.step >= 1 && stored.step <= 4) {
        setStep(stored.step);
      }
      if (stored.full_name) setFullName(stored.full_name);
      if (stored.selected_prompt) setSelectedPrompt(stored.selected_prompt);
    }
  }, [user, forceOpen, controlledOpen]);

  // Track step views (each step at most once per mount)
  useEffect(() => {
    if (!open) return;
    if (lastTrackedStep.current === step) return;
    lastTrackedStep.current = step;
    track("onboarding_step_viewed", { step });
  }, [open, step]);

  const handleOpenChange = (next: boolean): void => {
    if (controlledOpen !== undefined) {
      onOpenChange?.(next);
    } else {
      setInternalOpen(next);
    }
  };

  const persist = (patch: StoredState): void => {
    const merged = { ...readStored(), ...patch };
    writeStored(merged);
  };

  const handleSkipAll = (): void => {
    persist({ skipped: true, step });
    track("onboarding_skipped", { at_step: step });
    handleOpenChange(false);
  };

  const handleFinish = (): void => {
    persist({
      completed: true,
      step: 4,
      full_name: fullName,
      selected_prompt: selectedPrompt,
    });
    track("onboarding_completed", { used_prompt: !!selectedPrompt });
    handleOpenChange(false);
    if (selectedPrompt) {
      navigate(`/build?prompt=${encodeURIComponent(selectedPrompt)}`);
    } else {
      navigate("/build");
    }
  };

  const handleNext = (): void => {
    if (step < 4) {
      const nextStep = step + 1;
      persist({
        step: nextStep,
        full_name: fullName,
        selected_prompt: selectedPrompt,
      });
      setStep(nextStep);
    } else {
      handleFinish();
    }
  };

  const handleSaveName = async (): Promise<void> => {
    if (!user || !fullName.trim()) {
      handleNext();
      return;
    }
    setSavingName(true);
    try {
      const { error } = await supabase
        .from("profiles")
        .update({ full_name: fullName.trim() })
        .eq("id", user.id);
      if (error) {
        // Non-fatal — user can still proceed.
        // eslint-disable-next-line no-console
        console.warn("[onboarding] full_name save failed:", error.message);
      }
    } finally {
      setSavingName(false);
      handleNext();
    }
  };

  const handleVaultSetup = (): void => {
    persist({ step: 3 });
    track("onboarding_step_viewed", { step: 2, action: "vault_open" });
    handleOpenChange(false);
    navigate("/vault?from=onboarding");
  };

  // Generate the quote demo when entering step 4.
  useEffect(() => {
    if (!open || step !== 4) return;
    if (!selectedPrompt) return;
    setQuote(null);
    setQuoteError(null);
    setQuoteLoading(true);
    const ac = new AbortController();
    fetchQuote({ prompt: selectedPrompt, tier: "standard" }, ac.signal)
      .then((q) => {
        setQuote(q);
        track("quote_generated", {
          source: "onboarding",
          credits_min: q.credits_min,
          credits_max: q.credits_max,
        });
      })
      .catch((err: unknown) => {
        if ((err as Error).name === "AbortError") return;
        setQuoteError(
          err instanceof Error ? err.message : "Could not load a quote.",
        );
      })
      .finally(() => setQuoteLoading(false));
    return () => ac.abort();
  }, [open, step, selectedPrompt]);

  const titleId = useMemo(() => `onboarding-step-${step}-title`, [step]);

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-md" aria-labelledby={titleId}>
        <DialogHeader>
          <DialogTitle id={titleId}>
            {step === 1 && "Welcome to Deft"}
            {step === 2 && "Set up your Vault"}
            {step === 3 && "Pick something to build"}
            {step === 4 && "Your first quote"}
          </DialogTitle>
          <DialogDescription>
            {step === 1 && "We'll get you to your first build in under a minute."}
            {step === 2 && "Encrypted secrets you can reference safely in any prompt."}
            {step === 3 && "Try one of these to see Deft work autonomously."}
            {step === 4 && "Pre-flight estimate so you know exactly what you'll spend."}
          </DialogDescription>
        </DialogHeader>

        <div className="mt-2 flex items-center gap-1.5" aria-label={`Step ${step} of 4`}>
          {[1, 2, 3, 4].map((s) => (
            <div
              key={s}
              className={`h-1.5 flex-1 rounded-full transition-colors ${
                s <= step ? "bg-primary" : "bg-muted"
              }`}
              aria-hidden="true"
            />
          ))}
        </div>

        {step === 1 && (
          <div className="mt-4 space-y-3">
            <Label htmlFor="onb-name">What should we call you?</Label>
            <Input
              id="onb-name"
              autoFocus
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Your full name"
              aria-describedby="onb-name-hint"
              maxLength={120}
            />
            <p id="onb-name-hint" className="text-xs text-muted-foreground">
              Optional. We use this only inside the app.
            </p>
          </div>
        )}

        {step === 2 && (
          <div className="mt-4 space-y-4">
            <div className="rounded-md bg-secondary/50 p-4 text-sm leading-6 text-foreground">
              <div className="flex items-center gap-2 font-medium">
                <KeyRound size={14} aria-hidden="true" /> Zero-knowledge
              </div>
              <p className="mt-2 text-muted-foreground">
                Vault stores API keys encrypted on your device. Reference them as{" "}
                <code className="rounded bg-muted px-1 py-0.5 text-xs">$KEY_NAME</code>{" "}
                in any prompt — Deft injects them at runtime and redacts them from logs.
              </p>
            </div>
            <button
              type="button"
              onClick={handleVaultSetup}
              className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
            >
              Set up Vault now <ArrowRight size={14} aria-hidden="true" />
            </button>
          </div>
        )}

        {step === 3 && (
          <fieldset className="mt-4 space-y-2" aria-label="Suggested first prompts">
            <legend className="sr-only">Suggested prompts</legend>
            {SUGGESTED_PROMPTS.map((p) => (
              <label
                key={p}
                className={`flex cursor-pointer items-start gap-2 rounded-md border p-3 text-sm transition-colors ${
                  selectedPrompt === p
                    ? "border-primary bg-primary/5"
                    : "border-border hover:bg-secondary/40"
                }`}
              >
                <input
                  type="radio"
                  name="onb-prompt"
                  className="mt-0.5 accent-primary"
                  checked={selectedPrompt === p}
                  onChange={() => setSelectedPrompt(p)}
                />
                <span className="leading-6 text-foreground">{p}</span>
              </label>
            ))}
          </fieldset>
        )}

        {step === 4 && (
          <div className="mt-4 space-y-3 text-sm">
            <div className="rounded-md border border-border bg-card p-4">
              <p className="font-medium text-foreground">Prompt</p>
              <p className="mt-1 text-muted-foreground">{selectedPrompt}</p>
            </div>
            <div
              className="rounded-md border border-border bg-card p-4"
              aria-live="polite"
              aria-busy={quoteLoading}
            >
              {quoteLoading && (
                <div className="flex items-center gap-2 text-muted-foreground">
                  <Loader2 size={14} className="animate-spin" aria-hidden="true" />
                  Calculating estimate…
                </div>
              )}
              {quoteError && (
                <p className="text-destructive">Quote unavailable: {quoteError}</p>
              )}
              {quote && (
                <div className="space-y-1">
                  <div className="flex items-center justify-between">
                    <span className="text-muted-foreground">Tier</span>
                    <span className="font-medium text-foreground">{quote.tier}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-muted-foreground">Estimated cost</span>
                    <span className="font-medium text-foreground">
                      {formatCreditsRange(quote.credits_min, quote.credits_max)}{" "}
                      <span className="text-muted-foreground">
                        ({formatDollarsRange(quote.credits_min, quote.credits_max)})
                      </span>
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-muted-foreground">Estimated time</span>
                    <span className="font-medium text-foreground">
                      {formatEtaRange(quote.eta_seconds_min, quote.eta_seconds_max)}
                    </span>
                  </div>
                </div>
              )}
            </div>
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Sparkles size={12} aria-hidden="true" /> You can change tier and budget
              cap on the build page.
            </p>
          </div>
        )}

        <DialogFooter className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-between">
          <button
            type="button"
            onClick={handleSkipAll}
            className="text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
          >
            Skip everything
          </button>
          <div className="flex gap-2">
            {step > 1 && step < 4 && (
              <button
                type="button"
                onClick={() => setStep(step - 1)}
                className="rounded-md border border-border px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-secondary"
              >
                Back
              </button>
            )}
            {step === 1 && (
              <button
                type="button"
                onClick={handleSaveName}
                disabled={savingName}
                className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-60"
              >
                {savingName ? (
                  <Loader2 size={14} className="animate-spin" aria-hidden="true" />
                ) : (
                  <>Continue <ArrowRight size={14} aria-hidden="true" /></>
                )}
              </button>
            )}
            {step === 2 && (
              <button
                type="button"
                onClick={handleNext}
                className="rounded-md border border-border px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-secondary"
              >
                I'll do it later
              </button>
            )}
            {step === 3 && (
              <button
                type="button"
                onClick={handleNext}
                className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
              >
                Continue <ArrowRight size={14} aria-hidden="true" />
              </button>
            )}
            {step === 4 && (
              <button
                type="button"
                onClick={handleFinish}
                className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
              >
                Start building <Check size={14} aria-hidden="true" />
              </button>
            )}
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
