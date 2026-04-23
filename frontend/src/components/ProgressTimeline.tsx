import { useState, useEffect, useRef, useMemo } from "react";
import {
  Search,
  BarChart3,
  Code,
  FileText,
  CheckCircle2,
  AlertCircle,
  ChevronRight,
  Loader2,
  DollarSign,
  Beaker,
  Paperclip,
  GitBranch,
} from "lucide-react";

/* ------------------------------------------------------------------ */
/*  Types                                                             */
/* ------------------------------------------------------------------ */

export interface TimelineStep {
  id: string;
  type:
    | "step_start"
    | "step_complete"
    | "step_error"
    | "status_change"
    | "file_attached"
    | "cost_update"
    | "hypothesis_update"
    // BUG-F2-04: branch_update, checkpoint, and warning were missing from the
    // type union — TypeScript would accept them as string literals but
    // they're now explicit for type safety throughout the component.
    | "branch_update"
    | "checkpoint"
    | "warning"
    | "text";
  label: string;
  icon?: string;
  status: "running" | "complete" | "error";
  duration_ms?: number;
  detail?: string;
  timestamp: number;
  children?: TimelineStep[];
}

export interface StructuredEvent {
  type: string;
  step_id?: string;
  label?: string;
  icon?: string;
  message?: string;
  state?: string;
  duration_ms?: number;
  filename?: string;
  /** BUG-F2-05: file_attached spec includes a url field from the backend */
  url?: string;
  size?: number;
  mime?: string;
  spent_usd?: number;
  budget_usd?: number;
  raw_spent_usd?: number;
  id?: string;
  text?: string;
  status?: string;
  score?: number;
  content?: string;
  /** graph_update event — new or updated graph snapshot */
  nodes?: unknown[];
  edges?: unknown[];
  /** hypothesis_update fields */
  hypothesis_id?: string;
  /** branch_update fields */
  branch_id?: string;
  /** checkpoint fields */
  summary?: string;
  sources_count?: number;
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                           */
/* ------------------------------------------------------------------ */

const ICON_MAP: Record<string, React.FC<{ size?: number; className?: string }>> = {
  search: Search,
  analyze: BarChart3,
  code: Code,
  file: FileText,
  check: CheckCircle2,
  alert: AlertCircle,
  cost: DollarSign,
  hypothesis: Beaker,
  attachment: Paperclip,
  subagent: GitBranch,
};

function getIconForStep(iconName?: string, type?: string): React.FC<{ size?: number; className?: string }> {
  if (iconName && ICON_MAP[iconName]) return ICON_MAP[iconName];
  if (type === "file_attached") return Paperclip;
  if (type === "cost_update") return DollarSign;
  if (type === "hypothesis_update") return Beaker;
  if (type === "status_change") return BarChart3;
  return Search;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const remainder = s % 60;
  return remainder > 0 ? `${m}m ${remainder}s` : `${m}m`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Map a state name to an icon hint */
function stateToIcon(state: string): string {
  const lower = state.toLowerCase();
  if (lower.includes("search")) return "search";
  if (lower.includes("evaluat")) return "analyze";
  if (lower.includes("report")) return "file";
  if (lower.includes("deep")) return "search";
  if (lower.includes("tribunal") || lower.includes("skeptic")) return "analyze";
  if (lower.includes("checkpoint") || lower.includes("pivot")) return "code";
  return "search";
}

function makeEventInstanceId(prefix: string, ...parts: Array<string | number | undefined>): string {
  const stableParts = parts
    .filter((part): part is string | number => part !== undefined && part !== "")
    .map((part) => String(part).replace(/[^a-zA-Z0-9_-]+/g, "-"))
    .join("-");
  return `${prefix}${stableParts ? `-${stableParts}` : ""}-${Date.now()}-${Math.random()
    .toString(36)
    .slice(2, 8)}`;
}

/* ------------------------------------------------------------------ */
/*  Parse structured SSE event into TimelineStep                      */
/* ------------------------------------------------------------------ */

export function parseStructuredEvent(event: StructuredEvent, existingSteps: TimelineStep[]): TimelineStep | null {
  switch (event.type) {
    case "step_start":
      return {
        id: event.step_id || `step-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
        type: "step_start",
        label: event.label || "Processing...",
        icon: event.icon,
        status: "running",
        timestamp: Date.now(),
        detail: event.message,
      };

    case "step_complete": {
      // Find and update the matching step
      const target = existingSteps.find((s) => s.id === event.step_id);
      if (target) {
        return {
          ...target,
          type: "step_complete",
          status: "complete",
          duration_ms: event.duration_ms,
        };
      }
      return {
        id: event.step_id || `step-${Date.now()}`,
        type: "step_complete",
        label: event.label || "Step completed",
        icon: event.icon || "check",
        status: "complete",
        duration_ms: event.duration_ms,
        timestamp: Date.now(),
      };
    }

    case "step_error": {
      const errorTarget = existingSteps.find((s) => s.id === event.step_id);
      if (errorTarget) {
        return {
          ...errorTarget,
          type: "step_error",
          status: "error",
          detail: event.message,
        };
      }
      return {
        id: event.step_id || `step-${Date.now()}`,
        type: "step_error",
        label: event.label || "Error",
        icon: "alert",
        status: "error",
        detail: event.message,
        timestamp: Date.now(),
      };
    }

    case "status_change": {
      // Map internal state-machine values to user-friendly labels
      const FRIENDLY_STATE: Record<string, string | null> = {
        INIT: null,           // suppress — initializing already shown
        HALT: null,           // suppress — completion shown separately
        COMPLETED: null,
        HALTED: null,
        PENDING: null,
        RUNNING: null,
        SEARCHING: "Searching the web...",
        EVALUATING: "Evaluating findings...",
        REPORTING: "Generating report...",
        DEEP_DIVE: "Conducting deep dive...",
        CHECKPOINT: "Checkpoint — reviewing progress...",
        PIVOT: "Adjusting research direction...",
        TRIBUNAL: "Running adversarial review...",
        SKEPTIC_REVIEW: "Skeptic review in progress...",
      };
      const rawState = (event.state || "").toUpperCase();
      const friendlyLabel = event.message || (rawState in FRIENDLY_STATE ? FRIENDLY_STATE[rawState] : `State: ${event.state || "Unknown"}`);
      if (!friendlyLabel) return null; // suppress internal-only states
      return {
        id: makeEventInstanceId("state", event.state),
        type: "status_change",
        label: friendlyLabel,
        icon: stateToIcon(event.state || ""),
        status: "complete",
        timestamp: Date.now(),
      };
    }

    case "file_attached":
      return {
        id: makeEventInstanceId("file", event.filename),
        type: "file_attached",
        label: event.filename || "File attached",
        icon: "attachment",
        status: "complete",
        detail: event.size ? formatBytes(event.size) : undefined,
        timestamp: Date.now(),
      };

    case "cost_update":
      return {
        id: makeEventInstanceId("cost", event.spent_usd, event.budget_usd),
        type: "cost_update",
        label: `Cost: $${(event.spent_usd ?? 0).toFixed(2)} / $${(event.budget_usd ?? 0).toFixed(2)}`,
        icon: "cost",
        status: "complete",
        timestamp: Date.now(),
      };

    case "hypothesis_update":
      return {
        id: makeEventInstanceId("hyp", event.id ?? event.hypothesis_id, event.status),
        type: "hypothesis_update",
        label: event.text || "Hypothesis updated",
        icon: "hypothesis",
        status: event.status === "refuted" ? "error" : event.status === "confirmed" ? "complete" : "running",
        detail: event.status ? `Status: ${event.status}` : undefined,
        timestamp: Date.now(),
      };

    case "text": {
      const textContent = event.content || event.message || "";
      const isSubAgent = textContent.startsWith("[SubAgent]");
      return {
        id: makeEventInstanceId("text", isSubAgent ? "subagent" : "message"),
        type: "text",
        label: isSubAgent ? textContent.replace("[SubAgent] ", "") : textContent,
        icon: isSubAgent ? "subagent" : undefined,
        status: isSubAgent && textContent.includes("started") ? "running" as const
          : isSubAgent && textContent.includes("failed") ? "error" as const
          : "complete" as const,
        detail: isSubAgent ? "Sub-agent task" : undefined,
        timestamp: Date.now(),
      };
    }

    // BUG-F2-04: branch_update, checkpoint, and warning were never handled —
    // parseStructuredEvent returned null for all three, silently dropping them
    // from the timeline entirely.
    case "branch_update": {
      const branchLabel = event.branch_id
        ? `Branch ${event.branch_id}: ${event.status ?? "updated"}`
        : `Branch ${event.status ?? "updated"}`;
      const branchStatus = event.status === "failed" ? "error" as const : "complete" as const;
      return {
        id: makeEventInstanceId("branch", event.branch_id ?? "unknown", event.status ?? "updated", event.score ?? "na"),
        type: "branch_update",
        label: branchLabel,
        icon: "subagent",
        status: branchStatus,
        detail: event.score != null ? `Score: ${event.score}` : undefined,
        timestamp: Date.now(),
      };
    }

    case "checkpoint": {
      const cpDetail = event.sources_count != null
        ? `${event.sources_count} source${event.sources_count !== 1 ? "s" : ""} reviewed`
        : undefined;
      return {
        id: makeEventInstanceId("checkpoint", event.summary, event.sources_count),
        type: "checkpoint",
        label: event.summary || "Checkpoint",
        icon: "check",
        status: "complete" as const,
        detail: cpDetail,
        timestamp: Date.now(),
      };
    }

    case "warning":
      return {
        id: makeEventInstanceId("warning", event.message),
        type: "warning",
        label: event.message || "Warning",
        icon: "alert",
        status: "error" as const, // render as amber/error indicator
        timestamp: Date.now(),
      };

    default:
      return null;
  }
}

/* ------------------------------------------------------------------ */
/*  TimelineStepRow                                                   */
/* ------------------------------------------------------------------ */

function TimelineStepRow({
  step,
  onFileClick,
}: {
  step: TimelineStep;
  onFileClick?: (filename: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [liveMs, setLiveMs] = useState(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (step.status === "running") {
      intervalRef.current = setInterval(() => {
        setLiveMs(Date.now() - step.timestamp);
      }, 100);
    }
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [step.status, step.timestamp]);

  const Icon = getIconForStep(step.icon, step.type);
  const hasDetail = Boolean(step.detail) || (step.children && step.children.length > 0);
  const isFile = step.type === "file_attached";
  const isSubAgent = step.icon === "subagent";

  return (
    <div className={`group ${isSubAgent ? "ml-4 border-l border-blue-500/20 pl-2" : ""}`}>
      <button
        onClick={() => {
          if (isFile && onFileClick) {
            onFileClick(step.label);
          } else if (hasDetail) {
            setExpanded((e) => !e);
          }
        }}
        className={`flex w-full items-start gap-3 rounded-md px-2 py-1.5 text-left text-xs transition-colors ${
          hasDetail || isFile ? "hover:bg-secondary/50 cursor-pointer" : "cursor-default"
        } ${isSubAgent ? "bg-blue-500/5" : ""}`}
      >
        {/* Status indicator */}
        <div className="mt-0.5 shrink-0">
          {step.status === "running" ? (
            <Loader2 size={14} className="animate-spin text-blue-400" />
          ) : step.status === "error" ? (
            <AlertCircle size={14} className="text-red-400" />
          ) : (
            <CheckCircle2 size={14} className="text-green-400/70" />
          )}
        </div>

        {/* Icon */}
        <div className="mt-0.5 shrink-0">
          <Icon size={13} className="text-muted-foreground/70" />
        </div>

        {/* Label */}
        <div className="min-w-0 flex-1">
          <span
            className={`leading-relaxed ${
              step.status === "running"
                ? "text-foreground"
                : step.status === "error"
                ? "text-red-400"
                : "text-muted-foreground"
            } ${isFile ? "underline decoration-muted-foreground/30 underline-offset-2" : ""}`}
          >
            {step.label}
          </span>
        </div>

        {/* Duration / meta */}
        <div className="shrink-0 text-right">
          {step.status === "running" && (
            <span className="font-mono text-[10px] text-blue-400">
              {formatDuration(liveMs)}
            </span>
          )}
          {step.status !== "running" && step.duration_ms != null && (
            <span className="font-mono text-[10px] text-muted-foreground/50">
              {formatDuration(step.duration_ms)}
            </span>
          )}
          {isFile && step.detail && (
            <span className="text-[10px] text-muted-foreground/50">{step.detail}</span>
          )}
        </div>

        {/* Expand chevron */}
        {hasDetail && !isFile && (
          <ChevronRight
            size={12}
            className={`mt-0.5 shrink-0 text-muted-foreground/40 transition-transform ${
              expanded ? "rotate-90" : ""
            }`}
          />
        )}
      </button>

      {/* Expanded detail */}
      {expanded && step.detail && (
        <div className="ml-10 mt-1 rounded-md bg-secondary/30 px-3 py-2">
          <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-muted-foreground">
            {step.detail}
          </pre>
        </div>
      )}

      {/* Nested children */}
      {expanded && step.children && step.children.length > 0 && (
        <div className="ml-6 border-l border-border/40 pl-2">
          {step.children.map((child) => (
            <TimelineStepRow key={child.id} step={child} onFileClick={onFileClick} />
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  ProgressTimeline (main export)                                    */
/* ------------------------------------------------------------------ */

interface ProgressTimelineProps {
  steps: TimelineStep[];
  onFileClick?: (filename: string) => void;
}

export default function ProgressTimeline({ steps, onFileClick }: ProgressTimelineProps) {
  // Group steps by state/phase
  const groups = useGroupedSteps(steps);

  if (steps.length === 0) return null;

  return (
    <div className="space-y-1">
      {groups.map((group) => (
        <TimelineGroup key={group.id} group={group} onFileClick={onFileClick} />
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Grouping logic                                                    */
/* ------------------------------------------------------------------ */

interface StepGroup {
  id: string;
  label: string;
  steps: TimelineStep[];
  isActive: boolean;
}

function getGroupId(label: string, steps: TimelineStep[], index: number): string {
  const firstStep = steps[0];
  return `${firstStep?.id ?? label}-${index}`;
}

function makeStepGroup(label: string, steps: TimelineStep[], isActive: boolean, index: number): StepGroup {
  return {
    id: getGroupId(label, steps, index),
    label,
    steps,
    isActive,
  };
}

function finalizeGroup(group: Omit<StepGroup, "id">, index: number): StepGroup {
  return makeStepGroup(group.label, group.steps, group.isActive, index);
}

function createEmptyGroup(label: string): Omit<StepGroup, "id"> {
  return {
    label,
    steps: [],
    isActive: false,
  };
}

function createStatusGroup(step: TimelineStep): Omit<StepGroup, "id"> {
  return {
    label: step.label,
    steps: [step],
    isActive: step.status === "running",
  };
}

function getFallbackGroup(steps: TimelineStep[]): StepGroup {
  return makeStepGroup(
    "Progress",
    [...steps],
    steps.some((s) => s.status === "running"),
    0
  );
}

function pushCurrentGroup(groups: StepGroup[], currentGroup: Omit<StepGroup, "id"> | null): void {
  if (!currentGroup || currentGroup.steps.length === 0) return;
  groups.push(finalizeGroup(currentGroup, groups.length));
}

function ensureCurrentGroup(currentGroup: Omit<StepGroup, "id"> | null): Omit<StepGroup, "id"> {
  return currentGroup ?? createEmptyGroup("Task");
}

function addStepToGroup(
  currentGroup: Omit<StepGroup, "id"> | null,
  step: TimelineStep
): Omit<StepGroup, "id"> {
  const nextGroup = ensureCurrentGroup(currentGroup);
  nextGroup.steps.push(step);
  if (step.status === "running") {
    nextGroup.isActive = true;
  }
  return nextGroup;
}

function buildStepGroups(steps: TimelineStep[]): StepGroup[] {
  const groups: StepGroup[] = [];
  let currentGroup: Omit<StepGroup, "id"> | null = null;

  for (const step of steps) {
    if (step.type === "status_change") {
      pushCurrentGroup(groups, currentGroup);
      currentGroup = createStatusGroup(step);
    } else {
      currentGroup = addStepToGroup(currentGroup, step);
    }
  }

  pushCurrentGroup(groups, currentGroup);

  if (groups.length === 0 && steps.length > 0) {
    groups.push(getFallbackGroup(steps));
  }

  return groups;
}

// BUG-R7-05: Group keys were based only on group.label, so repeated status labels
// (for example, the same state reported more than once during reconnects or long
// investigations) caused duplicate React keys. React could then reuse the wrong
// TimelineGroup instance and leak collapsed/expanded UI state across distinct groups.
// Groups now get stable ids derived from their first step id, with an index fallback.

// BUG-R2-S2-06: Was using useCallback(..., [steps])() — creating a memoized function
// and immediately invoking it. useCallback is for memoizing function identity, not
// computed values. Replaced with useMemo which correctly memoizes the computed result.
function useGroupedSteps(steps: TimelineStep[]): StepGroup[] {
  return useMemo(() => buildStepGroups(steps), [steps]);
}

function TimelineGroup({
  group,
  onFileClick,
}: {
  group: StepGroup;
  onFileClick?: (filename: string) => void;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const nonStatusSteps = group.steps.filter((s) => s.type !== "status_change");

  return (
    <div className="border-l-2 border-accent/30 pl-3 py-1">
      {/* Group header (only if there are child steps beyond the status_change) */}
      {nonStatusSteps.length > 0 && (
        <>
          <button
            onClick={() => setCollapsed((c) => !c)}
            className="flex w-full items-center gap-2 py-1 text-left"
          >
            <ChevronRight
              size={11}
              className={`shrink-0 text-muted-foreground/50 transition-transform ${
                collapsed ? "" : "rotate-90"
              }`}
            />
            <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/60">
              {group.label}
            </span>
            {group.isActive && (
              <Loader2 size={10} className="animate-spin text-blue-400" />
            )}
            <span className="text-[10px] text-muted-foreground/40">
              {nonStatusSteps.length} step{nonStatusSteps.length !== 1 ? "s" : ""}
            </span>
          </button>
          {!collapsed && (
            <div className="space-y-0.5">
              {nonStatusSteps.map((step) => (
                <TimelineStepRow key={step.id} step={step} onFileClick={onFileClick} />
              ))}
            </div>
          )}
        </>
      )}
      {/* If group only has a status_change step, show it inline */}
      {nonStatusSteps.length === 0 && group.steps.length > 0 && (
        <div className="py-1 px-2 text-xs text-muted-foreground/60">
          {group.steps[0].label}
        </div>
      )}
    </div>
  );
}
