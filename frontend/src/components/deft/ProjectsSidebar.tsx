/**
 * F5 — Projects sidebar
 *
 * Lists the user's recent agent tasks (one task per row). The page treats
 * each task as a "project" for now — we'll add proper grouping in v1.1 once
 * the backend has a `projects` table.
 *
 * Behaviors:
 *  - "+ New run" CTA at top resets the page to an empty prompt
 *  - Shows current credit balance
 *  - Filterable by free-text search (client-side)
 *  - Clicking a row navigates to ?task=<id>
 *  - Auto-refreshes every 8s when present
 *  - Archive / Share read-only / Export / Delete from a kebab menu (P12)
 *  - "Show archived" toggle reveals soft-archived rows
 *
 * Mutations are optimistic — we update local state immediately and best-effort
 * call the backend. The backend endpoints for archive/share/export are
 * frontend-only stubs in this build (out of scope per the locked plan); the
 * real wiring lands in a later phase.
 */
import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Archive,
  Coins,
  FolderOpen,
  Loader2,
  Plus,
  Search,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import { ProjectRow, type ProjectRowData } from "./projects/ProjectRow";
import { ArchiveProjectDialog } from "./projects/ArchiveProjectDialog";
import { ShareProjectDialog } from "./projects/ShareProjectDialog";
import { ExportProjectDialog, type ExportFormat } from "./projects/ExportProjectDialog";
import { DeleteProjectDialog } from "./projects/DeleteProjectDialog";

interface AgentTaskRow {
  id: string;
  goal: string;
  state: string;
  selected_model: string;
  budget_usd: number;
  spent_usd: number;
  created_at: string;
  updated_at: string;
  has_final_answer: boolean;
  error: string | null;
  archived?: boolean;
}

interface AgentTaskList {
  total: number;
  limit: number;
  offset: number;
  tasks: AgentTaskRow[];
}

interface ProjectsSidebarProps {
  activeTaskId: string | null;
  onSelect: (taskId: string) => void;
  onNew: () => void;
  balance: number;
}

const POLL_INTERVAL_MS = 8_000;
const SHARE_BASE_URL =
  (typeof window !== "undefined" && window.location.origin) || "https://deft.computer";

type DialogKind = "archive" | "share" | "export" | "delete";

interface ActiveDialog {
  kind: DialogKind;
  task: ProjectRowData;
}

export function ProjectsSidebar({ activeTaskId, onSelect, onNew, balance }: ProjectsSidebarProps) {
  const [tasks, setTasks] = useState<AgentTaskRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [showArchived, setShowArchived] = useState(false);

  // Local override map: id → { archived?: boolean; deleted?: boolean }.
  // Lets the UI react instantly to mutations without waiting for the next poll.
  const [overrides, setOverrides] = useState<Record<string, { archived?: boolean; deleted?: boolean }>>({});

  const [dialog, setDialog] = useState<ActiveDialog | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const resp = await api.get<AgentTaskList>("/api/agent?limit=50");
        if (cancelled) return;
        setTasks(resp.tasks);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not load projects");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    const id = window.setInterval(load, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const merged = useMemo<ProjectRowData[]>(() => {
    return tasks
      .filter((t) => !overrides[t.id]?.deleted)
      .map((t) => ({
        id: t.id,
        goal: t.goal,
        state: t.state,
        created_at: t.created_at,
        spent_usd: t.spent_usd,
        archived: overrides[t.id]?.archived ?? t.archived ?? t.state === "archived",
      }));
  }, [tasks, overrides]);

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return merged.filter((t) => {
      if (!showArchived && t.archived) return false;
      if (q && !t.goal.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [merged, filter, showArchived]);

  const archivedCount = merged.filter((t) => t.archived).length;

  const closeDialog = () => {
    if (pending) return;
    setDialog(null);
  };

  const handleArchive = async () => {
    if (!dialog) return;
    setPending(true);
    setOverrides((prev) => ({ ...prev, [dialog.task.id]: { ...prev[dialog.task.id], archived: true } }));
    try {
      await api.post(`/api/agent/${dialog.task.id}/archive`, {}).catch(() => undefined);
    } finally {
      setPending(false);
      setDialog(null);
    }
  };

  const handleRestore = (task: ProjectRowData) => {
    setOverrides((prev) => ({ ...prev, [task.id]: { ...prev[task.id], archived: false } }));
    void api.post(`/api/agent/${task.id}/restore`, {}).catch(() => undefined);
  };

  const handleExport = async (format: ExportFormat) => {
    if (!dialog) return;
    setPending(true);
    try {
      // Frontend-only stub — emits a JSON snapshot from the row data so the
      // confirm-modal flow can be exercised end-to-end without a backend.
      const blob =
        format === "json"
          ? new Blob([JSON.stringify(dialog.task, null, 2)], { type: "application/json" })
          : new Blob(
              [
                `Run: ${dialog.task.goal}\nID: ${dialog.task.id}\nSpent: $${dialog.task.spent_usd.toFixed(2)}\n`,
              ],
              { type: "application/zip" },
            );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `deft-run-${dialog.task.id}.${format}`;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setPending(false);
      setDialog(null);
    }
  };

  const handleDelete = async () => {
    if (!dialog) return;
    setPending(true);
    setOverrides((prev) => ({ ...prev, [dialog.task.id]: { ...prev[dialog.task.id], deleted: true } }));
    try {
      await api.delete(`/api/agent/${dialog.task.id}`).catch(() => undefined);
    } finally {
      setPending(false);
      setDialog(null);
    }
  };

  const dialogTask = dialog?.task ?? null;

  return (
    <aside
      aria-label="Projects"
      className="flex h-full w-60 shrink-0 flex-col border-r border-border bg-[hsl(var(--sidebar-background))]"
    >
      <div className="border-b border-border px-3 py-3">
        <button
          type="button"
          onClick={onNew}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-accent-foreground transition-opacity hover:opacity-90"
        >
          <Plus size={14} aria-hidden /> New run
        </button>
        <div className="mt-3 flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
          <Coins size={12} aria-hidden />
          <span>
            <span className="font-mono text-foreground">{balance.toLocaleString()}</span> credits
          </span>
        </div>
      </div>

      <div className="border-b border-border px-3 py-2">
        <label className="relative block">
          <Search
            size={12}
            aria-hidden
            className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground"
          />
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter projects"
            aria-label="Filter projects"
            className="w-full rounded-md border border-border bg-input py-1.5 pl-7 pr-2 text-xs text-foreground outline-none placeholder:text-[hsl(var(--fg-3))] focus:border-accent"
          />
        </label>
        {archivedCount > 0 && (
          <button
            type="button"
            onClick={() => setShowArchived((v) => !v)}
            aria-pressed={showArchived}
            className={cn(
              "mt-2 inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-[10px] uppercase tracking-wide transition-colors",
              showArchived
                ? "text-foreground hover:text-muted-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Archive size={10} aria-hidden />
            {showArchived ? "Hide archived" : `Show archived (${archivedCount})`}
          </button>
        )}
      </div>

      <div className="flex-1 overflow-auto px-2 py-2">
        {loading ? (
          <div className="flex items-center justify-center gap-2 px-3 py-6 text-xs text-muted-foreground">
            <Loader2 size={12} className="animate-spin" aria-hidden /> Loading…
          </div>
        ) : error ? (
          <div className="flex items-start gap-2 px-2 py-3 text-xs text-destructive">
            <AlertTriangle size={12} className="mt-0.5 shrink-0" aria-hidden />
            <span>{error}</span>
          </div>
        ) : visible.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-1 px-3 py-10 text-center text-xs text-muted-foreground">
            <FolderOpen size={20} aria-hidden />
            <div>{filter ? "No matches" : "No runs yet"}</div>
            <div className="opacity-70">Start your first run from the prompt bar.</div>
          </div>
        ) : (
          <ul className="space-y-0.5">
            {visible.map((t) => (
              <ProjectRow
                key={t.id}
                task={t}
                active={activeTaskId === t.id}
                onSelect={() => onSelect(t.id)}
                onArchive={() => setDialog({ kind: "archive", task: t })}
                onRestore={() => handleRestore(t)}
                onShare={() => setDialog({ kind: "share", task: t })}
                onExport={() => setDialog({ kind: "export", task: t })}
                onDelete={() => setDialog({ kind: "delete", task: t })}
              />
            ))}
          </ul>
        )}
      </div>

      <ArchiveProjectDialog
        open={dialog?.kind === "archive"}
        onOpenChange={(o) => (!o ? closeDialog() : undefined)}
        projectName={dialogTask?.goal ?? ""}
        onConfirm={handleArchive}
        pending={pending}
      />
      <ShareProjectDialog
        open={dialog?.kind === "share"}
        onOpenChange={(o) => (!o ? closeDialog() : undefined)}
        projectName={dialogTask?.goal ?? ""}
        shareUrl={`${SHARE_BASE_URL}/r/${dialogTask?.id ?? ""}`}
      />
      <ExportProjectDialog
        open={dialog?.kind === "export"}
        onOpenChange={(o) => (!o ? closeDialog() : undefined)}
        projectName={dialogTask?.goal ?? ""}
        onExport={handleExport}
        pending={pending}
      />
      <DeleteProjectDialog
        open={dialog?.kind === "delete"}
        onOpenChange={(o) => (!o ? closeDialog() : undefined)}
        projectName={dialogTask?.goal ?? ""}
        onConfirm={handleDelete}
        pending={pending}
      />
    </aside>
  );
}
