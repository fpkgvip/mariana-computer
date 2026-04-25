/**
 * P13 — Skeleton loading rows for list surfaces.
 *
 * Replaces ad-hoc spinner blocks with a consistent shimmer of N rows so the
 * page reserves layout space and avoids a CLS jolt when data lands.
 */
import { cn } from "@/lib/utils";

export interface LoadingRowsProps {
  /** Number of skeleton rows to render. Default 3. */
  count?: number;
  /** Row height in tailwind class. Default "h-16". */
  rowClassName?: string;
  /** When true, the skeleton has no border or rounded corner — for sidebar lists. */
  bare?: boolean;
}

export function LoadingRows({
  count = 3,
  rowClassName = "h-16",
  bare = false,
}: LoadingRowsProps) {
  const rows = Array.from({ length: Math.max(1, count) }, (_, i) => i);
  return (
    <ul
      role="status"
      aria-label="Loading"
      aria-live="polite"
      aria-busy="true"
      className={cn("space-y-2", bare && "space-y-1.5")}
    >
      {rows.map((i) => (
        <li
          key={i}
          className={cn(
            "animate-pulse",
            rowClassName,
            bare
              ? "rounded-md bg-secondary/40"
              : "rounded-lg border border-border bg-card/40",
          )}
        />
      ))}
    </ul>
  );
}

/**
 * Inline spinner with optional label — for action-pending states (e.g.
 * inside a button being clicked, or on a small inline panel).
 */
export function InlineLoading({ label = "Loading" }: { label?: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-center justify-center gap-2 px-3 py-6 text-xs text-muted-foreground"
    >
      <span
        aria-hidden
        className="inline-block h-3 w-3 animate-spin rounded-full border-[1.5px] border-muted-foreground/40 border-t-muted-foreground"
      />
      <span>{label}</span>
    </div>
  );
}
