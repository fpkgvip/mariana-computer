export interface AgentProgressProps {
  state: string;
  currentStepIndex: number;
  totalSteps: number;
  fixAttempts?: number;
  replans?: number;
  elapsedSeconds?: number;
}

/**
 * Horizontal progress strip showing current agent phase
 * (PLAN/EXECUTE/TEST/FIX/REPLAN/DELIVER) plus step counter and elapsed time.
 */
export function AgentProgress({
  state,
  currentStepIndex,
  totalSteps,
  fixAttempts = 0,
  replans = 0,
  elapsedSeconds = 0,
}: AgentProgressProps) {
  const phases = ["PLAN", "EXECUTE", "TEST", "FIX", "REPLAN", "DELIVER"];
  const activeIdx = phases.indexOf(state.toUpperCase());
  const stepPct =
    totalSteps > 0 ? Math.min(100, Math.round((Math.max(0, currentStepIndex) / totalSteps) * 100)) : 0;
  const mm = Math.floor(elapsedSeconds / 60);
  const ss = Math.floor(elapsedSeconds % 60);

  return (
    <div className="rounded-lg border border-white/10 bg-white/[0.03] p-3 mb-3">
      <div className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-wider">
        {phases.map((p, i) => {
          const done = activeIdx > i;
          const active = activeIdx === i;
          return (
            <div
              key={p}
              className={`px-2 py-1 rounded-md border transition-colors ${
                active
                  ? "bg-emerald-500/20 border-emerald-400/60 text-emerald-200"
                  : done
                  ? "bg-white/5 border-white/15 text-white/50"
                  : "bg-transparent border-white/10 text-white/35"
              }`}
            >
              {p}
            </div>
          );
        })}
        <div className="ml-auto flex items-center gap-3 text-white/60 normal-case tracking-normal text-xs">
          <span>
            Step {Math.max(0, currentStepIndex) + (totalSteps > 0 ? 1 : 0)}/{totalSteps || "—"}
          </span>
          {fixAttempts > 0 ? <span className="text-amber-300">fixes: {fixAttempts}</span> : null}
          {replans > 0 ? <span className="text-sky-300">replans: {replans}</span> : null}
          <span className="tabular-nums">
            {mm}:{ss.toString().padStart(2, "0")}
          </span>
        </div>
      </div>
      <div className="mt-2 h-1 rounded-full bg-white/5 overflow-hidden">
        <div
          className="h-full bg-gradient-to-r from-emerald-500 to-sky-400 transition-all duration-500"
          style={{ width: `${stepPct}%` }}
        />
      </div>
    </div>
  );
}
