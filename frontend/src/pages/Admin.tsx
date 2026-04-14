import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { supabase } from "@/lib/supabase";
import { toast } from "sonner";
import {
  ShieldCheck,
  Users,
  Activity,
  RefreshCw,
  Loader2,
  ChevronDown,
  ChevronUp,
  Plus,
} from "lucide-react";

const API_URL = import.meta.env.VITE_API_URL ?? "";

async function getAccessToken(): Promise<string | null> {
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

/* ------------------------------------------------------------------ */
/*  Types                                                             */
/* ------------------------------------------------------------------ */

interface AdminStats {
  total_users: number;
  total_investigations: number;
  running_investigations: number;
  completed_investigations: number;
  failed_investigations: number;
  total_credits_consumed: number;
}

interface AdminUser {
  id: string;
  email: string;
  full_name: string | null;
  tokens: number;
  role: string;
  subscription_plan: string | null;
  subscription_status: string | null;
  created_at: string;
}

interface AdminInvestigation {
  task_id: string;
  topic: string;
  status: string;
  created_at: string;
  user_id: string;
  user_email?: string;
}

/* ------------------------------------------------------------------ */
/*  Sub-components                                                    */
/* ------------------------------------------------------------------ */

function StatCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="rounded-lg border border-border bg-card p-5">
      <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">{label}</p>
      <p className="mt-2 font-serif text-2xl font-semibold text-foreground">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Component                                                         */
/* ------------------------------------------------------------------ */

export default function Admin() {
  const { user } = useAuth();
  const navigate = useNavigate();

  const [stats, setStats] = useState<AdminStats | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [investigations, setInvestigations] = useState<AdminInvestigation[]>([]);
  const [loadingStats, setLoadingStats] = useState(true);
  const [loadingUsers, setLoadingUsers] = useState(true);
  const [loadingInvestigations, setLoadingInvestigations] = useState(true);

  // Add credits state
  const [addCreditUserId, setAddCreditUserId] = useState("");
  const [addCreditAmount, setAddCreditAmount] = useState("");
  const [isAddingCredits, setIsAddingCredits] = useState(false);

  // Collapsible sections
  const [showUsers, setShowUsers] = useState(true);
  const [showInvestigations, setShowInvestigations] = useState(true);

  /* Auth guard — admin only */
  useEffect(() => {
    if (user === null) return; // still loading
    if (!user || user.role !== "admin") {
      navigate("/chat", { replace: true });
    }
  }, [user, navigate]);

  const fetchStats = useCallback(async () => {
    setLoadingStats(true);
    try {
      const token = await getAccessToken();
      if (!token) return;
      const res = await fetch(`${API_URL}/api/admin/stats`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: AdminStats = await res.json();
      setStats(data);
    } catch (err) {
      console.error("[Admin] Failed to fetch stats:", err);
      toast.error("Could not load stats");
    } finally {
      setLoadingStats(false);
    }
  }, []);

  const fetchUsers = useCallback(async () => {
    setLoadingUsers(true);
    try {
      const token = await getAccessToken();
      if (!token) return;
      const res = await fetch(`${API_URL}/api/admin/users`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: AdminUser[] = await res.json();
      setUsers(data);
    } catch (err) {
      console.error("[Admin] Failed to fetch users:", err);
      toast.error("Could not load users");
    } finally {
      setLoadingUsers(false);
    }
  }, []);

  const fetchInvestigations = useCallback(async () => {
    setLoadingInvestigations(true);
    try {
      const token = await getAccessToken();
      if (!token) return;
      const res = await fetch(`${API_URL}/api/admin/investigations`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: AdminInvestigation[] = await res.json();
      setInvestigations(data);
    } catch (err) {
      console.error("[Admin] Failed to fetch investigations:", err);
      toast.error("Could not load investigations");
    } finally {
      setLoadingInvestigations(false);
    }
  }, []);

  useEffect(() => {
    if (!user || user.role !== "admin") return;
    fetchStats();
    fetchUsers();
    fetchInvestigations();
  }, [user, fetchStats, fetchUsers, fetchInvestigations]);

  const handleAddCredits = async (e: React.FormEvent) => {
    e.preventDefault();
    const amount = parseInt(addCreditAmount, 10);
    if (!addCreditUserId || isNaN(amount) || amount <= 0) {
      toast.error("Enter a valid user ID and credit amount.");
      return;
    }

    setIsAddingCredits(true);
    try {
      const token = await getAccessToken();
      if (!token) throw new Error("Not authenticated");

      const res = await fetch(`${API_URL}/api/admin/users/${addCreditUserId}/credits`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ amount }),
      });

      if (!res.ok) {
        const errText = await res.text().catch(() => res.statusText);
        throw new Error(`HTTP ${res.status}: ${errText}`);
      }

      toast.success(`Added ${amount} credits to user ${addCreditUserId}`);
      setAddCreditUserId("");
      setAddCreditAmount("");
      // Refresh user list to reflect new balance
      fetchUsers();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      toast.error("Failed to add credits", { description: msg });
    } finally {
      setIsAddingCredits(false);
    }
  };

  if (!user || user.role !== "admin") return null;

  return (
    <div className="min-h-screen bg-background px-4 py-10 sm:px-8 md:py-16">
      {/* Header */}
      <div className="mx-auto max-w-6xl">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <ShieldCheck size={22} className="text-primary" />
            <h1 className="font-serif text-2xl font-semibold text-foreground">Admin Panel</h1>
          </div>
          <button
            onClick={() => { fetchStats(); fetchUsers(); fetchInvestigations(); }}
            className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs text-foreground hover:bg-secondary transition-colors"
          >
            <RefreshCw size={12} /> Refresh
          </button>
        </div>

        {/* Stats */}
        <section className="mt-10">
          <div className="flex items-center gap-2 mb-4">
            <Activity size={15} className="text-muted-foreground" />
            <h2 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">System stats</h2>
          </div>
          {loadingStats ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 size={14} className="animate-spin" /> Loading stats...
            </div>
          ) : stats ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <StatCard label="Total users" value={stats.total_users.toLocaleString()} />
              <StatCard label="Total investigations" value={stats.total_investigations.toLocaleString()} />
              <StatCard label="Running" value={stats.running_investigations.toLocaleString()} />
              <StatCard label="Completed" value={stats.completed_investigations.toLocaleString()} />
              <StatCard label="Failed" value={stats.failed_investigations.toLocaleString()} />
              <StatCard
                label="Credits consumed"
                value={stats.total_credits_consumed.toLocaleString()}
                sub="across all users"
              />
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Stats unavailable.</p>
          )}
        </section>

        {/* Add credits */}
        <section className="mt-12">
          <div className="flex items-center gap-2 mb-4">
            <Plus size={15} className="text-muted-foreground" />
            <h2 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">Add credits</h2>
          </div>
          <form
            onSubmit={handleAddCredits}
            className="flex flex-wrap items-end gap-3 rounded-lg border border-border bg-card p-5"
          >
            <div className="flex-1 min-w-[200px]">
              <label className="mb-1 block text-xs text-muted-foreground">User ID</label>
              <input
                type="text"
                value={addCreditUserId}
                onChange={(e) => setAddCreditUserId(e.target.value)}
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/40 focus:border-primary focus:outline-none"
              />
            </div>
            <div className="w-32">
              <label className="mb-1 block text-xs text-muted-foreground">Credits</label>
              <input
                type="number"
                min="1"
                value={addCreditAmount}
                onChange={(e) => setAddCreditAmount(e.target.value)}
                placeholder="1000"
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/40 focus:border-primary focus:outline-none"
              />
            </div>
            <button
              type="submit"
              disabled={isAddingCredits}
              className="flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-60"
            >
              {isAddingCredits ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />}
              Add credits
            </button>
          </form>
        </section>

        {/* Users */}
        <section className="mt-12">
          <button
            onClick={() => setShowUsers((v) => !v)}
            className="flex w-full items-center justify-between gap-2 mb-4"
          >
            <div className="flex items-center gap-2">
              <Users size={15} className="text-muted-foreground" />
              <h2 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">
                Users {!loadingUsers && `(${users.length})`}
              </h2>
            </div>
            {showUsers ? <ChevronUp size={14} className="text-muted-foreground" /> : <ChevronDown size={14} className="text-muted-foreground" />}
          </button>

          {showUsers && (
            loadingUsers ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 size={14} className="animate-spin" /> Loading users...
              </div>
            ) : (
              <div className="overflow-x-auto rounded-lg border border-border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border bg-card/50">
                      <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Name</th>
                      <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Email</th>
                      <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Role</th>
                      <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Plan</th>
                      <th className="px-4 py-3 text-right text-xs font-semibold uppercase tracking-wider text-muted-foreground">Credits</th>
                      <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">ID</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {users.map((u) => (
                      <tr key={u.id} className="bg-card hover:bg-secondary/30 transition-colors">
                        <td className="px-4 py-3 text-foreground">{u.full_name ?? "—"}</td>
                        <td className="px-4 py-3 text-muted-foreground">{u.email}</td>
                        <td className="px-4 py-3">
                          <span className={`inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium ring-1 ring-inset ${
                            u.role === "admin"
                              ? "bg-primary/10 text-primary ring-primary/20"
                              : "bg-zinc-500/10 text-zinc-400 ring-zinc-500/20"
                          }`}>
                            {u.role}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-muted-foreground capitalize">
                          {u.subscription_plan ?? "none"}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-foreground">
                          {u.tokens.toLocaleString()}
                        </td>
                        <td className="px-4 py-3 font-mono text-[10px] text-muted-foreground/60">
                          {u.id}
                        </td>
                      </tr>
                    ))}
                    {users.length === 0 && (
                      <tr>
                        <td colSpan={6} className="px-4 py-6 text-center text-sm text-muted-foreground">
                          No users found.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            )
          )}
        </section>

        {/* Investigations */}
        <section className="mt-12">
          <button
            onClick={() => setShowInvestigations((v) => !v)}
            className="flex w-full items-center justify-between gap-2 mb-4"
          >
            <div className="flex items-center gap-2">
              <Activity size={15} className="text-muted-foreground" />
              <h2 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">
                All investigations {!loadingInvestigations && `(${investigations.length})`}
              </h2>
            </div>
            {showInvestigations ? <ChevronUp size={14} className="text-muted-foreground" /> : <ChevronDown size={14} className="text-muted-foreground" />}
          </button>

          {showInvestigations && (
            loadingInvestigations ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 size={14} className="animate-spin" /> Loading investigations...
              </div>
            ) : (
              <div className="overflow-x-auto rounded-lg border border-border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border bg-card/50">
                      <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Topic</th>
                      <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Status</th>
                      <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">User</th>
                      <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Created</th>
                      <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Task ID</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {investigations.map((inv) => (
                      <tr key={inv.task_id} className="bg-card hover:bg-secondary/30 transition-colors">
                        <td className="max-w-xs px-4 py-3 text-foreground truncate">{inv.topic}</td>
                        <td className="px-4 py-3">
                          <span className={`inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium ring-1 ring-inset ${
                            inv.status === "COMPLETED"
                              ? "bg-green-500/10 text-green-400 ring-green-500/20"
                              : inv.status === "RUNNING"
                              ? "bg-blue-500/10 text-blue-400 ring-blue-500/20"
                              : inv.status === "PENDING"
                              ? "bg-yellow-500/10 text-yellow-400 ring-yellow-500/20"
                              : "bg-red-500/10 text-red-400 ring-red-500/20"
                          }`}>
                            {inv.status}
                          </span>
                        </td>
                        <td className="px-4 py-3 font-mono text-[11px] text-muted-foreground">
                          {inv.user_email ?? inv.user_id.slice(0, 8) + "…"}
                        </td>
                        <td className="px-4 py-3 text-xs text-muted-foreground">
                          {new Date(inv.created_at).toLocaleString()}
                        </td>
                        <td className="px-4 py-3 font-mono text-[10px] text-muted-foreground/60">
                          {inv.task_id}
                        </td>
                      </tr>
                    ))}
                    {investigations.length === 0 && (
                      <tr>
                        <td colSpan={5} className="px-4 py-6 text-center text-sm text-muted-foreground">
                          No investigations found.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            )
          )}
        </section>
      </div>
    </div>
  );
}
