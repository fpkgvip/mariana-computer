/**
 * IdleStudio — the /build screen before a run is active.
 *
 * Centered prompt bar (the moment of intent) plus a Pre-flight card that
 * appears once the user has typed something. No hero badges, no all-caps
 * eyebrows, no exclamations — calm by design.
 */
import { PromptBar } from "@/components/deft/PromptBar";
import { PreflightCard } from "@/components/deft/PreflightCard";
import { BRAND } from "@/lib/brand";
import { cn } from "@/lib/utils";
import type { ModelTier, QuoteResponse } from "@/lib/agentApi";

interface IdleStudioProps {
  prompt: string;
  onPromptChange: (v: string) => void;
  onStart: (params: { tier: ModelTier; ceiling: number; quote: QuoteResponse }) => void | Promise<void>;
  balance: number;
  starting: boolean;
  unlimited?: boolean;
  /**
   * Optional: an active run is in flight in another pane (e.g. user is
   * looking at /build with no task selected but a sibling task is running).
   * Disables submit and shows calm copy.
   */
  runActive?: boolean;
  className?: string;
}

export function IdleStudio({
  prompt,
  onPromptChange,
  onStart,
  balance,
  starting,
  unlimited,
  runActive,
  className,
}: IdleStudioProps) {
  const trimmed = prompt.trim();

  return (
    <div className={cn("relative h-full overflow-auto", className)}>
      <div className="absolute inset-0 -z-0 bg-grid opacity-50" aria-hidden />
      <div className="absolute inset-0 -z-0 bg-vignette" aria-hidden />

      <div className="relative mx-auto flex w-full max-w-[820px] flex-col gap-6 px-5 py-12 sm:px-6 md:py-16">
        <header className="text-center">
          <h1 className="text-balance text-[28px] font-semibold leading-[1.15] tracking-[-0.02em] text-foreground sm:text-[34px]">
            Describe what you want.
          </h1>
          <p className="mx-auto mt-3 max-w-[440px] text-[14px] leading-[1.6] text-muted-foreground">
            {BRAND.name} plans, writes the code, runs it in a real browser, verifies it works,
            then deploys. You only pay for software that runs.
          </p>
        </header>

        <div className="flex flex-col gap-2">
          {runActive && (
            <p
              className="rounded-md border border-border/60 bg-surface-1/60 px-3 py-2 text-[12.5px] text-muted-foreground"
              role="status"
            >
              A run is active — cancel to start a new one.
            </p>
          )}
          <div
            className={cn(
              "rounded-2xl border border-border/80 bg-surface-1/85 p-3.5 shadow-elev-2 backdrop-blur-md",
              "focus-within:border-accent/60 focus-within:shadow-[0_0_0_4px_hsl(var(--accent)/0.10),0_18px_48px_-22px_hsl(var(--accent)/0.55)]",
              "transition-shadow duration-200",
            )}
          >
            <PromptBar
              initialValue={prompt}
              onChange={onPromptChange}
              onSubmit={async (p) => onPromptChange(p)}
              busy={starting}
              disabled={runActive}
              placeholder="A habit tracker with a streak heatmap and Supabase auth…"
            />
          </div>
        </div>

        {trimmed ? (
          <PreflightCard
            prompt={prompt}
            onStart={onStart}
            balance={balance}
            starting={starting}
            unlimited={unlimited}
          />
        ) : (
          <EmptyHint />
        )}
      </div>
    </div>
  );
}

function EmptyHint() {
  // A calm, non-marketing hint shown when the prompt is empty.  Three
  // example prompts the user can tap to fill the bar.  No exclamations,
  // calm prompt, no hype copy.
  const samples = [
    "A markdown editor with split-pane preview.",
    "A landing page for a payroll API, with a pricing table.",
    "A telegram bot that summarizes my unread emails every morning.",
  ];
  return (
    <div className="mx-auto w-full max-w-[640px] text-center">
      <p className="text-[12.5px] uppercase tracking-[0.16em] text-muted-foreground/80">
        For example
      </p>
      <ul className="mt-3 grid gap-2">
        {samples.map((s) => (
          <li
            key={s}
            className="rounded-lg border border-border/60 bg-surface-1/40 px-3 py-2 text-left text-[13px] text-foreground/80"
          >
            {s}
          </li>
        ))}
      </ul>
    </div>
  );
}
