/**
 * StudioHeader — the thin strip above the canvas.
 *
 * Shows:
 *  - Project / goal title (inline-editable on click, optional)
 *  - Stage chip (Plan / Write / Compile / Verify / Live)
 *  - Credits counter (burned vs ceiling)
 *  - Right-side actions (New run, Cancel)
 *
 * Calm: no all-caps, no exclamation marks, no hype adjectives.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Plus, Square } from "lucide-react";
import { cn } from "@/lib/utils";

export type StudioStage = "plan" | "write" | "compile" | "verify" | "live" | "idle" | "error";

const STAGE_LABEL: Record<StudioStage, string> = {
  plan: "Plan",
  write: "Write",
  compile: "Compile",
  verify: "Verify",
  live: "Live",
  idle: "Idle",
  error: "Error",
};

interface StudioHeaderProps {
  /** The goal / project title. Empty string renders muted placeholder. */
  title: string;
  onRename?: (next: string) => void;
  stage?: StudioStage;
  /** Credits already burned (integer). */
  spentCredits?: number;
  /** Credit ceiling for the run (integer, 0 = unlimited/admin). */
  ceilingCredits?: number;
  /** Wall time of the run in seconds (integer). */
  durationSec?: number;
  /** Show Cancel button when stage is not terminal. */
  canCancel?: boolean;
  onCancel?: () => void;
  /** "Start another" affordance. */
  onNewRun?: () => void;
  className?: string;
}

export function StudioHeader({
  title,
  onRename,
  stage = "idle",
  spentCredits,
  ceilingCredits,
  durationSec,
  canCancel,
  onCancel,
  onNewRun,
  className,
}: StudioHeaderProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(title);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => setDraft(title), [title]);

  useEffect(() => {
    if (editing) {
      requestAnimationFrame(() => inputRef.current?.focus());
      requestAnimationFrame(() => inputRef.current?.select());
    }
  }, [editing]);

  const commit = useCallback(() => {
    const next = draft.trim();
    if (!next || next === title) {
      setDraft(title);
      setEditing(false);
      return;
    }
    onRename?.(next);
    setEditing(false);
  }, [draft, title, onRename]);

  return (
    <div className={cn("flex items-center gap-3", className)}>
      {/* Stage chip */}
      <StageChip stage={stage} />

      {/* Title (inline-editable) */}
      <div className="min-w-0 flex-1">
        {editing && onRename ? (
          <input
            ref={inputRef}
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => {
              if (e.key === "Enter") commit();
              if (e.key === "Escape") {
                setDraft(title);
                setEditing(false);
              }
            }}
            className="w-full truncate rounded-md border border-border bg-input px-2 py-1 text-[13px] text-foreground outline-none focus:border-accent"
            aria-label="Project title"
          />
        ) : (
          <button
            type="button"
            onClick={() => onRename && setEditing(true)}
            className={cn(
              "block max-w-full truncate rounded-md px-1.5 py-0.5 text-left text-[13px] text-foreground",
              onRename
                ? "transition-colors hover:bg-secondary/60"
                : "cursor-default",
            )}
            title={title || undefined}
            aria-label={onRename ? "Rename project" : undefined}
          >
            {title || <span className="text-muted-foreground">Untitled run</span>}
          </button>
        )}
      </div>

      {/* Credits + time */}
      {(spentCredits !== undefined || durationSec !== undefined) && (
        <div className="hidden items-center gap-3 text-[11.5px] text-muted-foreground sm:flex">
          {spentCredits !== undefined && (
            <span className="tabular">
              <span className="font-mono text-foreground">{spentCredits.toLocaleString()}</span>
              {ceilingCredits ? (
                <span> / {ceilingCredits.toLocaleString()}</span>
              ) : null}{" "}
              credits
            </span>
          )}
          {durationSec !== undefined && (
            <span className="tabular font-mono">{formatDuration(durationSec)}</span>
          )}
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center gap-1.5">
        {canCancel && onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:border-destructive/50 hover:text-destructive"
            aria-label="Cancel run"
          >
            <Square size={11} aria-hidden /> Cancel
          </button>
        )}
        {onNewRun && (
          <button
            type="button"
            onClick={onNewRun}
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-2.5 py-1.5 text-[12px] font-medium text-accent-foreground transition-opacity hover:opacity-90"
          >
            <Plus size={11} aria-hidden /> New run
          </button>
        )}
      </div>
    </div>
  );
}

function StageChip({ stage }: { stage: StudioStage }) {
  // Colour key:
  //   plan/write/compile/verify → accent (active work)
  //   live                      → deploy/success
  //   error                     → destructive
  //   idle                      → muted
  const active = stage === "plan" || stage === "write" || stage === "compile" || stage === "verify";
  const cls = cn(
    "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium",
    stage === "live" && "border-deploy/50 bg-deploy/10 text-deploy",
    active && "border-accent/40 bg-[hsl(var(--accent-muted))] text-accent-strong",
    stage === "error" && "border-destructive/40 bg-destructive/10 text-destructive",
    stage === "idle" && "border-border bg-secondary text-muted-foreground",
  );
  return (
    <span className={cls} role="status" aria-label={`Stage: ${STAGE_LABEL[stage]}`}>
      <span
        className={cn(
          "size-1.5 rounded-full",
          stage === "live" && "bg-deploy",
          active && "bg-accent studio-breathe",
          stage === "error" && "bg-destructive",
          stage === "idle" && "bg-muted-foreground",
        )}
        aria-hidden
      />
      {STAGE_LABEL[stage]}
    </span>
  );
}

function formatDuration(totalSec: number): string {
  const s = Math.max(0, Math.round(totalSec));
  const m = Math.floor(s / 60);
  const sec = s % 60;
  if (m === 0) return `${sec}s`;
  if (m < 60) return `${m}m ${String(sec).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return `${h}h ${String(mm).padStart(2, "0")}m`;
}
