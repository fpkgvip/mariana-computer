import { useState } from "react";

export interface AgentPlanStep {
  id?: string;
  description: string;
  tool?: string;
}

export interface AgentPlanCardProps {
  goal: string;
  steps: AgentPlanStep[];
  currentStepIndex?: number;
  status?: string;
  onCancel?: () => void;
}

/**
 * Compact plan card shown at the top of an agent task.
 * Collapsible after first render so the terminal/progress takes focus.
 */
export function AgentPlanCard({
  goal,
  steps,
  currentStepIndex = -1,
  status = "PLANNED",
  onCancel,
}: AgentPlanCardProps) {
  const [collapsed, setCollapsed] = useState(false);
  const terminal = ["DONE", "FAILED", "HALTED"].includes(status);

  return (
    <div className="rounded-xl border border-white/10 bg-black/40 backdrop-blur p-4 mb-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-emerald-400/80">
            <span className="inline-block h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
            Computer · {status}
          </div>
          <h3 className="mt-1 text-base font-medium text-white/95 leading-snug break-words">
            {goal}
          </h3>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => setCollapsed((c) => !c)}
            className="text-xs text-white/60 hover:text-white px-2 py-1 rounded-md border border-white/10"
            aria-label={collapsed ? "Expand plan" : "Collapse plan"}
          >
            {collapsed ? "Show plan" : "Hide plan"}
          </button>
          {!terminal && onCancel ? (
            <button
              onClick={onCancel}
              className="text-xs text-red-300 hover:text-red-200 px-2 py-1 rounded-md border border-red-400/30 hover:border-red-400/60"
            >
              Stop
            </button>
          ) : null}
        </div>
      </div>

      {!collapsed && steps.length > 0 && (
        <ol className="mt-3 space-y-1.5">
          {steps.map((step, i) => {
            const done = i < currentStepIndex;
            const active = i === currentStepIndex;
            return (
              <li
                key={step.id ?? i}
                className={`flex items-start gap-3 text-sm rounded-md px-2 py-1.5 ${
                  active ? "bg-emerald-500/10 text-white" : done ? "text-white/50" : "text-white/70"
                }`}
              >
                <span
                  className={`mt-1 inline-flex h-4 w-4 items-center justify-center rounded-full text-[10px] font-semibold shrink-0 ${
                    done
                      ? "bg-emerald-500/30 text-emerald-200"
                      : active
                      ? "bg-emerald-500 text-black"
                      : "bg-white/10 text-white/60"
                  }`}
                >
                  {done ? "✓" : i + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="break-words">{step.description}</div>
                  {step.tool ? (
                    <code className="text-[11px] text-white/40 font-mono">{step.tool}</code>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
