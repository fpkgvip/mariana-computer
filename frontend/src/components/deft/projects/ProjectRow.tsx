/**
 * P12 — A single row in the projects sidebar.
 *
 * Composes the row's text + state dot + kebab menu. The archived state
 * dims the row to opacity-60 and adds an "archived" tag in the meta line.
 *
 * Pure presentation. The parent owns selection state and the action handlers.
 */
import { cn } from "@/lib/utils";
import { ProjectRowMenu } from "./ProjectRowMenu";

export interface ProjectRowData {
  id: string;
  goal: string;
  state: string;
  created_at: string;
  spent_usd: number;
  archived?: boolean;
}

interface ProjectRowProps {
  task: ProjectRowData;
  active: boolean;
  onSelect: () => void;
  onArchive: () => void;
  onRestore: () => void;
  onShare: () => void;
  onExport: () => void;
  onDelete: () => void;
  /** Force the kebab menu open — used by the dev preview for screenshots. */
  forceMenuOpen?: boolean;
}

export function ProjectRow({
  task,
  active,
  onSelect,
  onArchive,
  onRestore,
  onShare,
  onExport,
  onDelete,
  forceMenuOpen,
}: ProjectRowProps) {
  const archived = task.archived ?? task.state === "archived";

  return (
    <li>
      <div
        className={cn(
          "group flex w-full items-start gap-2 rounded-md px-2 py-2 transition-colors",
          active
            ? "bg-secondary text-foreground"
            : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground",
          archived && "opacity-60",
        )}
      >
        <button
          type="button"
          onClick={onSelect}
          aria-current={active ? "true" : undefined}
          className="flex min-w-0 flex-1 items-start gap-2 text-left text-xs"
        >
          <StateDot state={task.state} archived={archived} />
          <span className="min-w-0 flex-1">
            <span className="line-clamp-2 text-[12px] leading-tight text-foreground">
              {task.goal}
            </span>
            <span className="mt-1 flex items-center gap-1.5 text-[10px] text-muted-foreground">
              <span>{relativeTime(task.created_at)}</span>
              <span className="opacity-50">·</span>
              <span>${task.spent_usd.toFixed(2)}</span>
              {archived && (
                <>
                  <span className="opacity-50">·</span>
                  <span className="uppercase tracking-wide">archived</span>
                </>
              )}
            </span>
          </span>
        </button>
        <ProjectRowMenu
          archived={archived}
          onArchive={onArchive}
          onRestore={onRestore}
          onShare={onShare}
          onExport={onExport}
          onDelete={onDelete}
          open={forceMenuOpen}
          label={`Actions for ${task.goal}`}
        />
      </div>
    </li>
  );
}

function StateDot({ state, archived }: { state: string; archived: boolean }) {
  const cls = (() => {
    if (archived) return "bg-muted-foreground";
    switch (state) {
      case "done":
      case "completed":
        return "bg-success";
      case "failed":
      case "error":
        return "bg-destructive";
      case "stopped":
      case "cancelled":
        return "bg-muted-foreground";
      case "plan":
      case "act":
      case "running":
        return "bg-accent animate-pulse";
      default:
        return "bg-muted-foreground";
    }
  })();
  return <span className={cn("mt-1 h-2 w-2 shrink-0 rounded-full", cls)} aria-label={state} />;
}

function relativeTime(iso: string): string {
  try {
    const ts = new Date(iso).getTime();
    if (!Number.isFinite(ts)) return "";
    const diff = Math.floor((Date.now() - ts) / 1000);
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86_400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86_400)}d ago`;
  } catch {
    return "";
  }
}
