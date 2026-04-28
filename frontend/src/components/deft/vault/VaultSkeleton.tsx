/**
 * VaultSkeleton — placeholder that mirrors the unlocked vault layout.
 *
 * Replaces the previous full-page spinner. Reserves the same vertical space
 * as `<UnlockedBar />` + `<SecretsTable />` so the page does not jolt when
 * vault state arrives.
 *
 * Pulse rows + ARIA live region for screen readers (matches the
 * `LoadingState` primitive's pattern).
 */

export function VaultSkeleton() {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-busy="true"
      aria-label="Loading vault"
      className="space-y-5"
    >
      {/* Unlocked-bar placeholder */}
      <div className="flex items-center justify-between rounded-lg border border-border bg-card/40 px-4 py-3">
        <div className="flex items-center gap-3">
          <div className="h-2 w-2 animate-pulse rounded-full bg-muted" />
          <div className="h-3 w-40 animate-pulse rounded bg-muted" />
        </div>
        <div className="flex gap-2">
          <div className="h-7 w-20 animate-pulse rounded-md bg-muted" />
          <div className="h-7 w-20 animate-pulse rounded-md bg-muted" />
        </div>
      </div>

      {/* Secrets table placeholder */}
      <div className="rounded-lg border border-border bg-card/40">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="h-3 w-24 animate-pulse rounded bg-muted" />
          <div className="h-7 w-24 animate-pulse rounded-md bg-muted" />
        </div>
        <ul className="divide-y divide-border">
          {[0, 1, 2, 3].map((i) => (
            <li key={i} className="flex items-center justify-between px-4 py-3">
              <div className="flex flex-col gap-1.5">
                <div className="h-3 w-32 animate-pulse rounded bg-muted" />
                <div className="h-2.5 w-20 animate-pulse rounded bg-muted/70" />
              </div>
              <div className="flex items-center gap-2">
                <div className="h-3 w-24 animate-pulse rounded bg-muted/70" />
                <div className="h-7 w-7 animate-pulse rounded-md bg-muted" />
              </div>
            </li>
          ))}
        </ul>
      </div>

      <span className="sr-only">Loading vault</span>
    </div>
  );
}
