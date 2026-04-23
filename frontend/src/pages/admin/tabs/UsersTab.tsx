import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2, RefreshCw, Search } from "lucide-react";
import { toast } from "sonner";
import { adminApi, AdminUser } from "@/lib/adminApi";
import { SectionHeader } from "../AdminShell";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function UsersTab() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");
  const [roleFilter, setRoleFilter] = useState<string>("all");
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await adminApi.listUsers();
      setUsers(Array.isArray(data) ? data : []);
    } catch (err) {
      toast.error("Failed to load users", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const filtered = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return users.filter((u) => {
      if (roleFilter !== "all" && (u.role ?? "") !== roleFilter) return false;
      if (!f) return true;
      return (
        (u.email ?? "").toLowerCase().includes(f) ||
        (u.user_id ?? "").toLowerCase().includes(f)
      );
    });
  }, [users, filter, roleFilter]);

  async function handleRole(u: AdminUser, next: "user" | "admin" | "banned") {
    if (u.role === next) return;
    if (!confirm(`Change ${u.email ?? u.user_id} role to "${next}"?`)) return;
    setBusyId(u.user_id);
    try {
      await adminApi.setUserRole(u.user_id, next);
      toast.success(`Role updated to ${next}`);
      refresh();
    } catch (err) {
      toast.error("Failed to set role", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyId(null);
    }
  }

  async function handleCredits(u: AdminUser) {
    const raw = prompt(
      `Adjust credits for ${u.email ?? u.user_id}. Enter an integer. ` +
        `Positive = add, negative = subtract, or prefix with "=" to set absolute value.`,
      "0",
    );
    if (raw == null) return;
    const trimmed = raw.trim();
    if (!trimmed) return;
    let mode: "set" | "delta" = "delta";
    let numStr = trimmed;
    if (trimmed.startsWith("=")) {
      mode = "set";
      numStr = trimmed.slice(1);
    }
    const amount = parseInt(numStr, 10);
    if (isNaN(amount)) {
      toast.error("Invalid number");
      return;
    }
    if (mode === "set" && amount < 0) {
      toast.error("Absolute set must be >= 0");
      return;
    }
    const reason = prompt("Reason for this change (optional):", "") ?? undefined;
    setBusyId(u.user_id);
    try {
      const res = await adminApi.adjustCredits(u.user_id, mode, amount, reason);
      toast.success(`New balance: ${res.new_balance}`);
      refresh();
    } catch (err) {
      toast.error("Failed to adjust credits", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyId(null);
    }
  }

  async function handleSuspend(u: AdminUser) {
    const currentlySuspended = u.role === "banned";
    const next = !currentlySuspended;
    const reason = next
      ? prompt(`Reason for suspending ${u.email ?? u.user_id}?`, "") ?? undefined
      : undefined;
    if (next && reason == null) return;
    setBusyId(u.user_id);
    try {
      await adminApi.suspendUser(u.user_id, next, reason);
      toast.success(next ? "User suspended" : "User unsuspended");
      refresh();
    } catch (err) {
      toast.error("Failed to toggle suspension", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusyId(null);
    }
  }

  async function copyId(id: string) {
    if (!UUID_RE.test(id)) return;
    await navigator.clipboard.writeText(id);
    toast.success("User ID copied");
  }

  return (
    <div>
      <SectionHeader
        title={`Users (${filtered.length}/${users.length})`}
        action={
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
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[240px]">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <input
            className="w-full rounded-md border border-border bg-background py-2 pl-8 pr-3 text-sm"
            placeholder="Search by email or user ID…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
        <select
          className="rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={roleFilter}
          onChange={(e) => setRoleFilter(e.target.value)}
        >
          <option value="all">All roles</option>
          <option value="user">user</option>
          <option value="admin">admin</option>
          <option value="banned">banned</option>
        </select>
      </div>

      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-muted/40 text-left">
            <tr>
              <th className="px-3 py-2">Email</th>
              <th className="px-3 py-2">Role</th>
              <th className="px-3 py-2">Credits</th>
              <th className="px-3 py-2">Plan</th>
              <th className="px-3 py-2">Joined</th>
              <th className="px-3 py-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-muted-foreground">
                  <Loader2 className="mx-auto h-4 w-4 animate-spin" />
                </td>
              </tr>
            )}
            {!loading && filtered.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-muted-foreground">
                  No users match.
                </td>
              </tr>
            )}
            {filtered.map((u) => (
              <tr key={u.user_id} className="border-t border-border">
                <td className="px-3 py-2">
                  <div className="font-medium">{u.email ?? "—"}</div>
                  <button
                    className="text-xs text-muted-foreground hover:text-foreground"
                    onClick={() => copyId(u.user_id)}
                    title="Copy user ID"
                  >
                    {u.user_id}
                  </button>
                </td>
                <td className="px-3 py-2">
                  <span
                    className={`rounded-md px-2 py-0.5 text-xs font-medium ${
                      u.role === "admin"
                        ? "bg-primary/15 text-primary"
                        : u.role === "banned"
                        ? "bg-destructive/15 text-destructive"
                        : "bg-muted text-muted-foreground"
                    }`}
                  >
                    {u.role ?? "user"}
                  </span>
                </td>
                <td className="px-3 py-2 font-mono">{u.credits}</td>
                <td className="px-3 py-2">
                  {u.subscription_plan ?? "—"}
                  {u.subscription_status && (
                    <span className="ml-1 text-xs text-muted-foreground">
                      ({u.subscription_status})
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 text-xs text-muted-foreground">
                  {u.created_at ? new Date(u.created_at).toLocaleDateString() : "—"}
                </td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    <button
                      disabled={busyId === u.user_id}
                      onClick={() => handleCredits(u)}
                      className="rounded-md border border-border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
                    >
                      Credits
                    </button>
                    <button
                      disabled={busyId === u.user_id}
                      onClick={() =>
                        handleRole(u, u.role === "admin" ? "user" : "admin")
                      }
                      className="rounded-md border border-border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
                    >
                      {u.role === "admin" ? "Demote" : "Promote"}
                    </button>
                    <button
                      disabled={busyId === u.user_id}
                      onClick={() => handleSuspend(u)}
                      className="rounded-md border border-border px-2 py-1 text-xs text-destructive hover:bg-destructive/10 disabled:opacity-50"
                    >
                      {u.role === "banned" ? "Unsuspend" : "Suspend"}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
