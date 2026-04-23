import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2, Plus, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { adminApi, InternalAdminTask } from "@/lib/adminApi";
import { SectionHeader } from "../AdminShell";

const STATUS_ORDER = ["todo", "in_progress", "blocked", "done"];
const STATUS_COLORS: Record<string, string> = {
  todo: "bg-muted text-muted-foreground",
  in_progress: "bg-amber-500/15 text-amber-500",
  blocked: "bg-destructive/15 text-destructive",
  done: "bg-emerald-500/15 text-emerald-500",
};
const PRIORITY_COLORS: Record<string, string> = {
  P0: "border-destructive text-destructive",
  P1: "border-amber-500 text-amber-500",
  P2: "border-border text-muted-foreground",
  P3: "border-border text-muted-foreground",
};

export function TodoTab() {
  const [tasks, setTasks] = useState<InternalAdminTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [categoryFilter, setCategoryFilter] = useState<string>("");
  const [priorityFilter, setPriorityFilter] = useState<string>("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState("ops");
  const [priority, setPriority] = useState("P2");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await adminApi.listInternalTasks({
        status: statusFilter || undefined,
        category: categoryFilter || undefined,
        priority: priorityFilter || undefined,
      });
      setTasks(Array.isArray(data) ? data : []);
    } catch (err) {
      toast.error("Failed to load admin todo", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setLoading(false);
    }
  }, [statusFilter, categoryFilter, priorityFilter]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const categories = useMemo(() => {
    const s = new Set<string>();
    tasks.forEach((t) => t.category && s.add(t.category));
    return [...s].sort();
  }, [tasks]);

  async function updateStatus(t: InternalAdminTask, status: string) {
    setBusyId(t.id);
    try {
      await adminApi.patchInternalTask(t.id, { status });
      toast.success(`"${t.title}" → ${status}`);
      refresh();
    } catch (err) {
      toast.error("Failed to update", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyId(null);
    }
  }

  async function remove(t: InternalAdminTask) {
    if (!confirm(`Delete task "${t.title}"?`)) return;
    setBusyId(t.id);
    try {
      await adminApi.deleteInternalTask(t.id);
      toast.success("Deleted");
      refresh();
    } catch (err) {
      toast.error("Failed to delete", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyId(null);
    }
  }

  async function create(e: React.FormEvent) {
    e.preventDefault();
    const t = title.trim();
    if (!t) {
      toast.error("Title required");
      return;
    }
    try {
      await adminApi.createInternalTask({
        title: t,
        description: description.trim() || null,
        category,
        priority,
        status: "todo",
      });
      toast.success("Task created");
      setTitle("");
      setDescription("");
      setCategory("ops");
      setPriority("P2");
      setShowForm(false);
      refresh();
    } catch (err) {
      toast.error("Failed to create", {
        description: err instanceof Error ? err.message : String(err),
      });
    }
  }

  const byStatus = useMemo(() => {
    const map: Record<string, InternalAdminTask[]> = {};
    STATUS_ORDER.forEach((s) => (map[s] = []));
    for (const t of tasks) {
      const s = t.status ?? "todo";
      (map[s] ??= []).push(t);
    }
    return map;
  }, [tasks]);

  return (
    <div>
      <SectionHeader
        title={`Admin todo (${tasks.length})`}
        action={
          <div className="flex gap-2">
            <button
              onClick={() => setShowForm((v) => !v)}
              className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90"
            >
              <Plus className="h-4 w-4" />
              {showForm ? "Cancel" : "Add task"}
            </button>
            <button
              onClick={refresh}
              disabled={loading}
              className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
            >
              {loading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="h-4 w-4" />
              )}
              Refresh
            </button>
          </div>
        }
      />

      {showForm && (
        <form
          onSubmit={create}
          className="mb-6 space-y-3 rounded-lg border border-dashed border-border bg-card/50 p-4"
        >
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Title"
            className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          />
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Description (optional)"
            rows={3}
            className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          />
          <div className="flex flex-wrap gap-3">
            <label className="text-sm">
              Category:{" "}
              <input
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                className="rounded-md border border-border bg-background px-2 py-1 text-sm"
              />
            </label>
            <label className="text-sm">
              Priority:{" "}
              <select
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
                className="rounded-md border border-border bg-background px-2 py-1 text-sm"
              >
                <option>P0</option>
                <option>P1</option>
                <option>P2</option>
                <option>P3</option>
              </select>
            </label>
            <button
              type="submit"
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90"
            >
              Create
            </button>
          </div>
        </form>
      )}

      <div className="mb-4 flex flex-wrap gap-2">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-2 text-sm"
        >
          <option value="">All statuses</option>
          {STATUS_ORDER.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select
          value={priorityFilter}
          onChange={(e) => setPriorityFilter(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-2 text-sm"
        >
          <option value="">All priorities</option>
          <option>P0</option>
          <option>P1</option>
          <option>P2</option>
          <option>P3</option>
        </select>
        <select
          value={categoryFilter}
          onChange={(e) => setCategoryFilter(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-2 text-sm"
        >
          <option value="">All categories</option>
          {categories.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </div>

      <div className="grid gap-4 lg:grid-cols-4">
        {STATUS_ORDER.map((s) => (
          <div
            key={s}
            className="rounded-lg border border-border bg-card/50 p-3"
          >
            <h3 className="mb-2 flex items-center justify-between text-sm font-semibold uppercase tracking-wider text-muted-foreground">
              <span>{s.replace("_", " ")}</span>
              <span className="rounded bg-muted px-1.5 py-0.5 text-xs">
                {byStatus[s]?.length ?? 0}
              </span>
            </h3>
            <div className="space-y-2">
              {(byStatus[s] ?? []).map((t) => (
                <div
                  key={t.id}
                  className="rounded-md border border-border bg-background p-3"
                >
                  <div className="flex items-center gap-2">
                    <span
                      className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${
                        PRIORITY_COLORS[t.priority ?? "P2"] ?? ""
                      }`}
                    >
                      {t.priority ?? "P2"}
                    </span>
                    {t.category && (
                      <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                        {t.category}
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-sm font-medium leading-snug">
                    {t.title}
                  </p>
                  {t.description && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      {t.description}
                    </p>
                  )}
                  <div className="mt-2 flex flex-wrap gap-1">
                    {STATUS_ORDER.filter((x) => x !== s).map((next) => (
                      <button
                        key={next}
                        disabled={busyId === t.id}
                        onClick={() => updateStatus(t, next)}
                        className={`rounded px-1.5 py-0.5 text-[10px] ${STATUS_COLORS[next]}`}
                      >
                        → {next.replace("_", " ")}
                      </button>
                    ))}
                    <button
                      disabled={busyId === t.id}
                      onClick={() => remove(t)}
                      className="ml-auto rounded px-1 text-destructive hover:bg-destructive/10"
                      title="Delete"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                </div>
              ))}
              {(byStatus[s]?.length ?? 0) === 0 && (
                <p className="py-4 text-center text-xs text-muted-foreground">
                  Empty
                </p>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
