/**
 * ResearchFlowchart — visual preview of the research architecture plan.
 *
 * Renders a DAG of research phases as connected nodes, a hypothesis list,
 * data sources, and risk factors.  Designed to sit inside the plan-approval
 * card in Chat.tsx.
 *
 * No external dependencies beyond React + Tailwind + lucide-react.
 */

import {
  GitBranch,
  Search,
  BarChart3,
  FileText,
  ShieldAlert,
  Layers,
  Database,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Target,
  ArrowDown,
} from "lucide-react";
import { useState } from "react";

/* ------------------------------------------------------------------ */
/*  Types (must mirror the backend ResearchArchitecturePlan)            */
/* ------------------------------------------------------------------ */

export interface ArchitectureHypothesis {
  statement: string;
  priority: number;
  test_strategy: string;
}

export interface ArchitecturePhase {
  name: string;
  description: string;
  depends_on: string[];
}

export interface ResearchArchitecturePlan {
  hypotheses: ArchitectureHypothesis[];
  data_sources: string[];
  research_phases: ArchitecturePhase[];
  estimated_branches: number;
  risk_factors: string[];
  flow_description: string;
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

const PHASE_ICONS: Record<string, React.ElementType> = {
  Architecture: Layers,
  "Hypothesis Generation": GitBranch,
  "Evidence Search": Search,
  "Analysis & Scoring": BarChart3,
  "Iterative Deepening": Target,
  "Adversarial Review": ShieldAlert,
  "Report Synthesis": FileText,
};

const PHASE_COLORS: Record<string, string> = {
  Architecture: "bg-blue-50 border-blue-200 text-blue-700",
  "Hypothesis Generation": "bg-violet-50 border-violet-200 text-violet-700",
  "Evidence Search": "bg-amber-50 border-amber-200 text-amber-700",
  "Analysis & Scoring": "bg-emerald-50 border-emerald-200 text-emerald-700",
  "Iterative Deepening": "bg-cyan-50 border-cyan-200 text-cyan-700",
  "Adversarial Review": "bg-rose-50 border-rose-200 text-rose-700",
  "Report Synthesis": "bg-indigo-50 border-indigo-200 text-indigo-700",
};

function priorityLabel(p: number): string {
  if (p >= 9) return "Critical";
  if (p >= 7) return "High";
  if (p >= 5) return "Medium";
  return "Low";
}

function priorityColor(p: number): string {
  if (p >= 9) return "bg-red-100 text-red-700";
  if (p >= 7) return "bg-amber-100 text-amber-700";
  if (p >= 5) return "bg-blue-100 text-blue-700";
  return "bg-gray-100 text-gray-600";
}

/* ------------------------------------------------------------------ */
/*  Components                                                         */
/* ------------------------------------------------------------------ */

function PhaseNode({ phase, isLast }: { phase: ArchitecturePhase; isLast: boolean }) {
  const Icon = PHASE_ICONS[phase.name] || Layers;
  const colorClass = PHASE_COLORS[phase.name] || "bg-gray-50 border-gray-200 text-gray-700";

  return (
    <div className="flex flex-col items-center">
      <div
        className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-medium ${colorClass} w-full max-w-[260px] shadow-sm`}
      >
        <Icon size={14} className="shrink-0" />
        <div className="min-w-0">
          <div className="font-semibold text-[11px] leading-tight">{phase.name}</div>
          <div className="text-[10px] opacity-70 leading-snug mt-0.5">{phase.description}</div>
        </div>
      </div>
      {!isLast && (
        <div className="flex flex-col items-center py-1">
          <div className="h-3 w-px bg-border" />
          <ArrowDown size={10} className="text-muted-foreground -my-0.5" />
        </div>
      )}
    </div>
  );
}

function HypothesisRow({ hyp, index }: { hyp: ArchitectureHypothesis; index: number }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div
      className="border border-border rounded-md px-3 py-2 cursor-pointer hover:bg-secondary/50 transition-colors"
      onClick={() => setExpanded(!expanded)}
    >
      <div className="flex items-start gap-2">
        <span className="text-[10px] font-mono text-muted-foreground mt-0.5">
          H{index + 1}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-[11px] leading-snug text-foreground">{hyp.statement}</span>
            <span
              className={`shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-medium ${priorityColor(hyp.priority)}`}
            >
              {priorityLabel(hyp.priority)}
            </span>
          </div>
          {expanded && (
            <div className="mt-1.5 text-[10px] text-muted-foreground leading-relaxed">
              <span className="font-medium">Strategy:</span> {hyp.test_strategy}
            </div>
          )}
        </div>
        {expanded ? (
          <ChevronUp size={12} className="text-muted-foreground shrink-0 mt-0.5" />
        ) : (
          <ChevronDown size={12} className="text-muted-foreground shrink-0 mt-0.5" />
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main export                                                        */
/* ------------------------------------------------------------------ */

export default function ResearchFlowchart({
  architecture,
}: {
  architecture: ResearchArchitecturePlan;
}) {
  const { hypotheses, data_sources, research_phases, risk_factors, estimated_branches } =
    architecture;

  return (
    <div className="space-y-4">
      {/* ── Flow Description ─────────────────────────────────────────── */}
      <div className="text-[10px] font-medium uppercase tracking-[0.15em] text-muted-foreground">
        Research Flow
      </div>

      {/* ── Phase DAG ────────────────────────────────────────────────── */}
      <div className="flex flex-col items-center">
        {research_phases.map((phase, i) => (
          <PhaseNode key={phase.name} phase={phase} isLast={i === research_phases.length - 1} />
        ))}
      </div>

      {/* ── Branches indicator ───────────────────────────────────────── */}
      <div className="flex items-center justify-center gap-2 py-1">
        <GitBranch size={12} className="text-muted-foreground" />
        <span className="text-[10px] text-muted-foreground">
          {estimated_branches} parallel research {estimated_branches === 1 ? "branch" : "branches"}
        </span>
      </div>

      {/* ── Hypotheses ───────────────────────────────────────────────── */}
      <div>
        <div className="text-[10px] font-medium uppercase tracking-[0.15em] text-muted-foreground mb-2">
          Hypotheses to Test
        </div>
        <div className="space-y-1.5">
          {hypotheses
            .sort((a, b) => b.priority - a.priority)
            .map((hyp, i) => (
              <HypothesisRow key={i} hyp={hyp} index={i} />
            ))}
        </div>
      </div>

      {/* ── Data Sources ─────────────────────────────────────────────── */}
      <div>
        <div className="text-[10px] font-medium uppercase tracking-[0.15em] text-muted-foreground mb-1.5">
          Data Sources
        </div>
        <div className="flex flex-wrap gap-1.5">
          {data_sources.map((src, i) => (
            <span
              key={i}
              className="inline-flex items-center gap-1 rounded-full border border-border bg-secondary/50 px-2 py-0.5 text-[10px] text-muted-foreground"
            >
              <Database size={9} />
              {src}
            </span>
          ))}
        </div>
      </div>

      {/* ── Risk Factors ─────────────────────────────────────────────── */}
      {risk_factors.length > 0 && (
        <div>
          <div className="text-[10px] font-medium uppercase tracking-[0.15em] text-muted-foreground mb-1.5">
            Risk Factors
          </div>
          <div className="space-y-1">
            {risk_factors.map((risk, i) => (
              <div
                key={i}
                className="flex items-start gap-1.5 text-[10px] text-muted-foreground"
              >
                <AlertTriangle size={10} className="text-amber-500 shrink-0 mt-0.5" />
                <span>{risk}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
