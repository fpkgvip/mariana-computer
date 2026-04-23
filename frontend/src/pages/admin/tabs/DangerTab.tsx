import { useState } from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { adminApi } from "@/lib/adminApi";
import { SectionHeader } from "../AdminShell";

const CONFIRM = "I UNDERSTAND";

export function DangerTab({
  frozen,
  onFrozenChange,
}: {
  frozen: boolean;
  onFrozenChange: (f: boolean) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [freezeMsg, setFreezeMsg] = useState(
    "Mariana is temporarily paused for maintenance. Please check back soon.",
  );

  async function toggleFreeze() {
    const next = !frozen;
    const reason = prompt(
      next ? "Why are you freezing the system?" : "Why are you unfreezing?",
      "",
    );
    if (reason == null) return;
    setBusy("freeze");
    try {
      await adminApi.setSystemFreeze(next, reason, next ? freezeMsg : null);
      toast.success(next ? "System frozen" : "System unfrozen");
      onFrozenChange(next);
    } catch (err) {
      toast.error("Failed to toggle freeze", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(null);
    }
  }

  async function flushRedis() {
    const ok = prompt(
      `This wipes Redis (cache + queues). Type "${CONFIRM}" to confirm.`,
      "",
    );
    if (ok !== CONFIRM) {
      if (ok != null) toast.error("Cancelled — phrase did not match");
      return;
    }
    setBusy("flush");
    try {
      await adminApi.dangerFlushRedis(CONFIRM);
      toast.success("Redis flushed");
    } catch (err) {
      toast.error("Flush failed", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(null);
    }
  }

  async function haltRunning() {
    const ok = prompt(
      `This halts ALL RUNNING tasks. Type "${CONFIRM}" to confirm.`,
      "",
    );
    if (ok !== CONFIRM) {
      if (ok != null) toast.error("Cancelled — phrase did not match");
      return;
    }
    setBusy("halt");
    try {
      const res = await adminApi.dangerHaltRunning(CONFIRM);
      toast.success(`Halted ${res.halted} tasks`);
    } catch (err) {
      toast.error("Halt failed", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <SectionHeader title="Danger zone" />

      <div className="mb-6 flex items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/5 p-4">
        <AlertTriangle className="h-5 w-5 shrink-0 text-destructive" />
        <p className="text-sm text-destructive">
          These controls take effect immediately across the production
          cluster. Every action is recorded in the audit log with your user ID.
        </p>
      </div>

      <div className="space-y-4">
        {/* Kill switch */}
        <div className="rounded-lg border border-border bg-card p-5">
          <h3 className="font-semibold">System-wide kill switch</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            When frozen, the API rejects new task submissions and the frontend
            shows the message below. Existing running tasks continue unless
            halted separately.
          </p>
          <label className="mt-3 block text-xs font-medium">
            Message shown to users while frozen
          </label>
          <textarea
            value={freezeMsg}
            onChange={(e) => setFreezeMsg(e.target.value)}
            rows={2}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          />
          <button
            disabled={busy === "freeze"}
            onClick={toggleFreeze}
            className={`mt-3 inline-flex items-center gap-2 rounded-md px-4 py-2 text-sm font-medium ${
              frozen
                ? "bg-emerald-500 text-white hover:opacity-90"
                : "bg-destructive text-destructive-foreground hover:opacity-90"
            } disabled:opacity-50`}
          >
            {busy === "freeze" && <Loader2 className="h-4 w-4 animate-spin" />}
            {frozen ? "Unfreeze system" : "Freeze system"}
          </button>
        </div>

        {/* Halt running */}
        <div className="rounded-lg border border-border bg-card p-5">
          <h3 className="font-semibold">Halt all running tasks</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            Marks every currently RUNNING task as HALTED. Workers will observe
            the change on their next heartbeat and stop execution.
          </p>
          <button
            disabled={busy === "halt"}
            onClick={haltRunning}
            className="mt-3 inline-flex items-center gap-2 rounded-md bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:opacity-90 disabled:opacity-50"
          >
            {busy === "halt" && <Loader2 className="h-4 w-4 animate-spin" />}
            Halt all RUNNING tasks
          </button>
        </div>

        {/* Flush Redis */}
        <div className="rounded-lg border border-border bg-card p-5">
          <h3 className="font-semibold">Flush Redis</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            Wipes all keys in the current Redis DB (cache, queues, rate-limit
            counters, SSE pub/sub). Only use when recovering from a corrupted
            state — expect a brief service blip.
          </p>
          <button
            disabled={busy === "flush"}
            onClick={flushRedis}
            className="mt-3 inline-flex items-center gap-2 rounded-md bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:opacity-90 disabled:opacity-50"
          >
            {busy === "flush" && <Loader2 className="h-4 w-4 animate-spin" />}
            Flush Redis
          </button>
        </div>
      </div>
    </div>
  );
}
