/**
 * Stage derivation — single source of truth shared by StudioHeader, the
 * five-stage rail in LiveCanvas, and the PreviewPane waiting state.
 *
 * Stages, in order:
 *   plan    — drafting the plan
 *   write   — writing files
 *   compile — compiling / type-check / bundle
 *   verify  — running the app for real
 *   live    — deploy succeeded; iframe is live
 *
 * Plus terminal/utility stages:
 *   idle  — no run yet
 *   error — the run failed before deploy
 */
import type { AgentEvent, AgentPreviewManifest, AgentTaskState } from "@/lib/agentRunApi";
import type { StudioStage } from "./StudioHeader";

export const STAGE_ORDER: ReadonlyArray<Exclude<StudioStage, "idle" | "error">> = [
  "plan",
  "write",
  "compile",
  "verify",
  "live",
];

export const STAGE_CAPTION: Record<StudioStage, string> = {
  plan: "Breaking your goal into ordered steps.",
  write: "Generating files in the sandbox.",
  compile: "Type-checking and bundling.",
  verify: "Running it in a real browser.",
  live: "Live preview is up.",
  idle: "Type a prompt to start.",
  error: "The run ended before deploy.",
};

const TERMINAL_STATES = new Set([
  "done",
  "completed",
  "failed",
  "stopped",
  "cancelled",
  "error",
]);

/**
 * Walk recent events backward to find the most likely current stage.
 * Falls back to the task's own state when events aren't conclusive.
 */
export function deriveStudioStage(
  task: AgentTaskState | null,
  events: AgentEvent[],
  manifest?: AgentPreviewManifest | null,
): StudioStage {
  if (!task) return "idle";

  const failed = task.state === "failed" || task.state === "error";
  if (failed && !manifest?.deployed) return "error";

  if (manifest?.deployed) return "live";

  // Walk back through the last 80 events to identify the active stage.
  const window = events.slice(-80);
  for (let i = window.length - 1; i >= 0; i--) {
    const e = window[i];
    const tool = (e.payload?.tool ?? e.payload?.tool_name ?? "") as string;
    const evType = e.event_type;

    if (tool === "deploy_preview" || evType === "deploy_started") return "verify";
    if (tool === "browser_screenshot" || evType === "verify" || tool === "browser_open") {
      return "verify";
    }
    if (
      tool === "sandbox_exec" ||
      tool === "exec" ||
      /\b(?:vite|tsc|npm|pnpm|build|compile)\b/.test(JSON.stringify(e.payload ?? {}))
    ) {
      return "compile";
    }
    if (tool === "fs_write" || tool === "fs_edit" || evType === "step_started") return "write";
    if (evType === "plan" || tool === "plan") return "plan";
  }

  // No conclusive event yet — fall back to task state.
  if (TERMINAL_STATES.has(task.state)) return "idle";
  return "plan";
}

/**
 * Canonical floor for the up-front credit reservation made by
 * ``POST /api/agent``. The backend reserves
 * ``max(CREDITS_MIN_RESERVATION, int(budget_usd*100))`` and the Pydantic
 * ``AgentStartRequest.budget_usd`` field enforces ``ge=1.0`` so direct-API
 * callers cannot bypass the floor either. The PreflightCard clamps its
 * ceiling slider/input to this minimum so the UI never displays a sub-floor
 * ceiling that would silently over-reserve at start time.
 *
 * 100 credits == $1.00 under the canonical 1 credit = $0.01 conversion.
 */
export const CREDITS_MIN_RESERVATION = 100;

/**
 * Convenience: integer credit count from the dollar-denominated task fields.
 * 1 credit == $0.01.
 */
export function creditsFromUsd(usd: number | undefined | null): number {
  if (!usd || !Number.isFinite(usd)) return 0;
  return Math.max(0, Math.round(usd * 100));
}

/**
 * Convenience: seconds elapsed since `task.started_at` (or `created_at`).
 */
export function durationSeconds(task: AgentTaskState | null): number {
  if (!task) return 0;
  const startedRaw =
    (task as Record<string, unknown>).started_at ??
    (task as Record<string, unknown>).created_at;
  if (typeof startedRaw !== "string") return 0;
  const started = new Date(startedRaw).getTime();
  if (!Number.isFinite(started)) return 0;
  const endedRaw = (task as Record<string, unknown>).ended_at;
  const ended =
    typeof endedRaw === "string" && Number.isFinite(new Date(endedRaw).getTime())
      ? new Date(endedRaw).getTime()
      : Date.now();
  return Math.max(0, Math.round((ended - started) / 1000));
}

/**
 * Whether the run is in a state where Cancel makes sense.
 */
export function canCancelStage(stage: StudioStage): boolean {
  return stage === "plan" || stage === "write" || stage === "compile" || stage === "verify";
}
