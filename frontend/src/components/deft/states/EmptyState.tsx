/**
 * P13 — Calm empty state for any list-shaped surface.
 *
 * Pattern: thin dashed border, centered icon in a soft ring, headline,
 * supporting line, optional primary CTA. Used wherever a list is genuinely
 * empty (no runs, no skills, no secrets) versus filtered to nothing — the
 * `filtered` prop swaps to a "No matches" voice without changing layout.
 */
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface EmptyStateProps {
  icon: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
  /** When true, the visual is denser (sidebar / inline) — no dashed card. */
  dense?: boolean;
  /** When true, indicates the empty state is the result of a filter, not a true zero. */
  filtered?: boolean;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  dense = false,
  filtered = false,
}: EmptyStateProps) {
  if (dense) {
    return (
      <div
        role="status"
        aria-live="polite"
        className="flex flex-col items-center justify-center gap-1 px-3 py-8 text-center text-xs text-muted-foreground"
      >
        <span className="text-muted-foreground" aria-hidden>
          {icon}
        </span>
        <div className="text-foreground/80">{title}</div>
        {description ? <div>{description}</div> : null}
        {action ? <div className="mt-1.5">{action}</div> : null}
      </div>
    );
  }
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "flex flex-col items-center justify-center rounded-lg border border-dashed border-border/70 bg-card/30 px-6 py-12 text-center",
        filtered && "border-border/40",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "flex h-12 w-12 items-center justify-center rounded-full",
          filtered ? "bg-muted text-muted-foreground" : "bg-accent/10 text-accent",
        )}
      >
        {icon}
      </span>
      <h3 className="mt-4 text-base font-semibold text-foreground">{title}</h3>
      {description ? (
        <p className="mt-2 max-w-xs text-sm leading-relaxed text-muted-foreground">
          {description}
        </p>
      ) : null}
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}
