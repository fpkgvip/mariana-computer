/**
 * Dev-only preview for the P12 Projects archive + share + export surfaces.
 *
 * Drives the new ProjectRow + ProjectRowMenu + dialog primitives with mock
 * data so we can capture screenshots across viewport widths without standing
 * up the API or Auth context. Gated on import.meta.env.DEV in App.tsx.
 *
 * Modes (?mode=):
 *  - default        — sidebar with mixed states, archived rows hidden
 *  - kebab_open     — sidebar with the kebab menu force-opened on row 1
 *  - archived       — sidebar with "Show archived" toggled on
 *  - archive_modal  — Archive confirm dialog open over the sidebar
 *  - share_modal    — Share read-only dialog open
 *  - export_modal   — Export run dialog open
 *  - delete_modal   — Delete confirm dialog open
 *  - empty          — no projects yet
 */
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Archive, Coins, FolderOpen, Plus, Search } from "lucide-react";
import { Navbar } from "@/components/Navbar";
import { cn } from "@/lib/utils";
import { ProjectRow, type ProjectRowData } from "@/components/deft/projects/ProjectRow";
import { ArchiveProjectDialog } from "@/components/deft/projects/ArchiveProjectDialog";
import { ShareProjectDialog } from "@/components/deft/projects/ShareProjectDialog";
import { ExportProjectDialog } from "@/components/deft/projects/ExportProjectDialog";
import { DeleteProjectDialog } from "@/components/deft/projects/DeleteProjectDialog";

type Mode =
  | "default"
  | "kebab_open"
  | "archived"
  | "archive_modal"
  | "share_modal"
  | "export_modal"
  | "delete_modal"
  | "empty";

const MOCK_TASKS: ProjectRowData[] = [
  {
    id: "tsk_dev_1",
    goal: "Habit tracker with streak heatmap and Supabase auth",
    state: "running",
    created_at: new Date(Date.now() - 4 * 60 * 1000).toISOString(),
    spent_usd: 1.42,
  },
  {
    id: "tsk_dev_2",
    goal: "Markdown editor with split-pane preview",
    state: "done",
    created_at: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
    spent_usd: 3.1,
  },
  {
    id: "tsk_dev_3",
    goal: "Telegram bot summarizing unread emails",
    state: "failed",
    created_at: new Date(Date.now() - 26 * 60 * 60 * 1000).toISOString(),
    spent_usd: 0.84,
  },
  {
    id: "tsk_dev_4",
    goal: "Landing page for a payroll API",
    state: "done",
    created_at: new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString(),
    spent_usd: 2.2,
    archived: true,
  },
  {
    id: "tsk_dev_5",
    goal: "Slack reminder bot for standup",
    state: "stopped",
    created_at: new Date(Date.now() - 5 * 24 * 60 * 60 * 1000).toISOString(),
    spent_usd: 0.41,
    archived: true,
  },
];

export default function DevProjects() {
  if (!import.meta.env.DEV) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-foreground">
        <p className="text-sm text-muted-foreground">Dev preview disabled in production.</p>
      </div>
    );
  }

  const [params, setParams] = useSearchParams();
  const mode = (params.get("mode") as Mode | null) ?? "default";

  const goto = (m: Mode) => {
    const next = new URLSearchParams(params);
    next.set("mode", m);
    setParams(next, { replace: true });
  };

  const showArchived = mode === "archived";
  const empty = mode === "empty";
  const tasks = empty
    ? []
    : MOCK_TASKS.filter((t) => (showArchived ? true : !t.archived));

  const archivedCount = MOCK_TASKS.filter((t) => t.archived).length;

  // Pick the row to attach an open kebab/dialog to.
  const focusTask = MOCK_TASKS[0];

  // Stub setters — required by the dialog props but no-op in the dev preview.
  const [pending] = useState(false);

  const modes: Mode[] = useMemo(
    () => [
      "default",
      "kebab_open",
      "archived",
      "archive_modal",
      "share_modal",
      "export_modal",
      "delete_modal",
      "empty",
    ],
    [],
  );

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <Navbar />
      <div className="mt-16 border-b border-border/60 bg-surface-1/40 px-3 py-2">
        <div className="mx-auto flex max-w-[1440px] flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
          <span className="font-mono uppercase tracking-[0.16em]">/dev/projects</span>
          <span aria-hidden>·</span>
          {modes.map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => goto(m)}
              className={cn(
                "rounded px-2 py-0.5 font-mono uppercase tracking-[0.12em] transition-colors",
                mode === m
                  ? "bg-accent/20 text-accent"
                  : "text-muted-foreground hover:bg-secondary hover:text-foreground",
              )}
            >
              {m}
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-1 min-h-0 overflow-hidden">
        <aside
          aria-label="Projects"
          className="flex h-full w-60 shrink-0 flex-col border-r border-border bg-[hsl(var(--sidebar-background))]"
        >
          <div className="border-b border-border px-3 py-3">
            <button
              type="button"
              className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-accent-foreground transition-opacity hover:opacity-90"
            >
              <Plus size={14} aria-hidden /> New run
            </button>
            <div className="mt-3 flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
              <Coins size={12} aria-hidden />
              <span>
                <span className="font-mono text-foreground">4,280</span> credits
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
                placeholder="Filter projects"
                aria-label="Filter projects"
                className="w-full rounded-md border border-border bg-input py-1.5 pl-7 pr-2 text-xs text-foreground outline-none placeholder:text-[hsl(var(--fg-3))] focus:border-accent"
              />
            </label>
            {archivedCount > 0 && (
              <button
                type="button"
                aria-pressed={showArchived}
                onClick={() => goto(showArchived ? "default" : "archived")}
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
            {tasks.length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-1 px-3 py-10 text-center text-xs text-muted-foreground">
                <FolderOpen size={20} aria-hidden />
                <div>No runs yet</div>
                <div className="opacity-70">Start your first run from the prompt bar.</div>
              </div>
            ) : (
              <ul className="space-y-0.5">
                {tasks.map((t, idx) => (
                  <ProjectRow
                    key={t.id}
                    task={t}
                    active={idx === 0 && !t.archived}
                    onSelect={() => undefined}
                    onArchive={() => goto("archive_modal")}
                    onRestore={() => goto("default")}
                    onShare={() => goto("share_modal")}
                    onExport={() => goto("export_modal")}
                    onDelete={() => goto("delete_modal")}
                    forceMenuOpen={mode === "kebab_open" && idx === 0}
                  />
                ))}
              </ul>
            )}
          </div>
        </aside>

        <main className="flex-1 overflow-auto px-6 py-10 text-xs text-muted-foreground">
          <p>Open the kebab on a row to archive, share read-only, export, or delete.</p>
          <p className="mt-2 opacity-70">
            Mode: <span className="font-mono text-foreground">{mode}</span>
          </p>
        </main>
      </div>

      <ArchiveProjectDialog
        open={mode === "archive_modal"}
        onOpenChange={(o) => (!o ? goto("default") : undefined)}
        projectName={focusTask.goal}
        onConfirm={() => goto("default")}
        pending={pending}
      />
      <ShareProjectDialog
        open={mode === "share_modal"}
        onOpenChange={(o) => (!o ? goto("default") : undefined)}
        projectName={focusTask.goal}
        shareUrl={`https://deft.computer/r/${focusTask.id}`}
        onRevoke={() => goto("default")}
      />
      <ExportProjectDialog
        open={mode === "export_modal"}
        onOpenChange={(o) => (!o ? goto("default") : undefined)}
        projectName={focusTask.goal}
        onExport={() => goto("default")}
        pending={pending}
      />
      <DeleteProjectDialog
        open={mode === "delete_modal"}
        onOpenChange={(o) => (!o ? goto("default") : undefined)}
        projectName={focusTask.goal}
        onConfirm={() => goto("default")}
        pending={pending}
      />
    </div>
  );
}
