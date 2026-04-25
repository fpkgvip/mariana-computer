/**
 * UnlockedBar — top status bar shown when the vault is unlocked.
 *
 * Communicates session state and exposes lock + delete controls.  Calm copy,
 * destructive control held in muted tone unless hovered.
 */
import { Lock, ShieldCheck, Trash2, Clock } from "lucide-react";
import { Button } from "@/components/ui/button";

export interface UnlockedBarProps {
  /** ISO time the session will auto-lock at, if known. */
  autoLockAt?: string | null;
  /** Minutes of inactivity before auto-lock kicks in. */
  autoLockMinutes?: number;
  onLock: () => void;
  onRequestDestroy: () => void;
}

function formatDuration(ms: number): string {
  if (ms <= 0) return "now";
  const m = Math.round(ms / 60_000);
  if (m < 1) return "moments";
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return rem ? `${h}h ${rem}m` : `${h}h`;
}

export function UnlockedBar({
  autoLockAt,
  autoLockMinutes = 30,
  onLock,
  onRequestDestroy,
}: UnlockedBarProps) {
  const lockIn = autoLockAt
    ? formatDuration(new Date(autoLockAt).getTime() - Date.now())
    : null;

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border/70 bg-surface-1/60 px-4 py-3">
      <div className="flex items-center gap-2 text-[13px]">
        <span className="inline-flex h-6 w-6 items-center justify-center rounded-md bg-emerald-500/15 text-emerald-300">
          <ShieldCheck size={13} aria-hidden />
        </span>
        <span className="font-medium text-foreground">Vault unlocked</span>
        <span className="hidden items-center gap-1 text-[11.5px] text-muted-foreground sm:inline-flex">
          <Clock size={11} aria-hidden />
          {lockIn ? `auto-locks in ${lockIn}` : `auto-locks after ${autoLockMinutes} min idle`}
        </span>
      </div>
      <div className="flex gap-2">
        <Button size="sm" variant="outline" onClick={onLock}>
          <Lock size={13} className="mr-1.5" aria-hidden />
          Lock now
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={onRequestDestroy}
          className="text-rose-300 hover:bg-rose-500/10 hover:text-rose-200"
        >
          <Trash2 size={13} className="mr-1.5" aria-hidden />
          Delete vault
        </Button>
      </div>
    </div>
  );
}
