/**
 * Admin.tsx — v3.7 comprehensive admin console.
 *
 * 10 tabs:
 *   Overview · Users · Tasks · Usage & Costs · Models · Feature Flags ·
 *   Audit Log · System Health · Admin Todo · Danger Zone
 *
 * Auth: client-side role gate + server-side verification on mount (GET
 * /api/admin/overview).  The RPC functions behind every admin endpoint
 * re-check the caller's admin role in Postgres (SECURITY DEFINER), so the
 * UI gate is defense-in-depth, not the primary control.
 */
import { useEffect, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { Loader2, ShieldAlert } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/contexts/AuthContext";
import { adminApi } from "@/lib/adminApi";
import { AdminShell } from "./admin/AdminShell";
import { OverviewTab } from "./admin/tabs/OverviewTab";
import { UsersTab } from "./admin/tabs/UsersTab";
import { TasksTab } from "./admin/tabs/TasksTab";
import { UsageTab } from "./admin/tabs/UsageTab";
import { ModelsTab } from "./admin/tabs/ModelsTab";
import { FlagsTab } from "./admin/tabs/FlagsTab";
import { AuditTab } from "./admin/tabs/AuditTab";
import { HealthTab } from "./admin/tabs/HealthTab";
import { TodoTab } from "./admin/tabs/TodoTab";
import { DangerTab } from "./admin/tabs/DangerTab";

const VALID_TABS = new Set([
  "overview",
  "users",
  "tasks",
  "usage",
  "models",
  "flags",
  "audit",
  "health",
  "todo",
  "danger",
]);

export default function Admin() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const initialTab = (() => {
    const hash = location.hash.replace("#", "");
    return VALID_TABS.has(hash) ? hash : "overview";
  })();
  const [activeTab, setActiveTab] = useState(initialTab);
  const [adminVerified, setAdminVerified] = useState(false);
  const [verifying, setVerifying] = useState(true);
  const [frozen, setFrozen] = useState(false);

  // Keep URL hash in sync with active tab (deep-linkable).
  useEffect(() => {
    if (location.hash !== `#${activeTab}`) {
      window.history.replaceState(null, "", `#${activeTab}`);
    }
  }, [activeTab, location.hash]);

  // Client-side gate — redirect non-admins fast. Then verify server-side.
  useEffect(() => {
    if (!user) return;
    if (user.role !== "admin") {
      navigate("/build", { replace: true });
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const data = await adminApi.overview();
        if (cancelled) return;
        setAdminVerified(true);
        setFrozen(Boolean(data?.frozen));
      } catch (err) {
        if (cancelled) return;
        toast.error("Admin access denied by server", {
          description: err instanceof Error ? err.message : String(err),
        });
        navigate("/build", { replace: true });
      } finally {
        if (!cancelled) setVerifying(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [user, navigate]);

  if (!user) {
    // Pre-auth skeleton: header + sidebar + table placeholders so the page
    // reserves layout space instead of jolting from a centered spinner to the
    // full admin shell when the user object lands.
    return (
      <div
        role="status"
        aria-live="polite"
        aria-busy="true"
        aria-label="Loading admin console"
        className="min-h-screen bg-background"
      >
        <div className="border-b border-border bg-card">
          <div className="mx-auto flex max-w-[1400px] items-center justify-between px-6 py-4">
            <div className="flex items-center gap-3">
              <div className="h-6 w-6 animate-pulse rounded-md bg-muted" />
              <div className="h-4 w-32 animate-pulse rounded bg-muted" />
            </div>
            <div className="h-7 w-24 animate-pulse rounded-md bg-muted" />
          </div>
        </div>
        <div className="mx-auto max-w-[1400px] px-6 py-6">
          <div className="flex gap-6">
            <nav className="hidden w-44 shrink-0 space-y-1 sm:block">
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <div
                  key={i}
                  className="h-7 w-full animate-pulse rounded-md bg-muted/70"
                />
              ))}
            </nav>
            <div className="flex-1 space-y-3">
              <div className="h-9 w-48 animate-pulse rounded bg-muted" />
              <div className="rounded-lg border border-border bg-card/40">
                <div className="h-10 border-b border-border" />
                {[0, 1, 2, 3, 4].map((i) => (
                  <div
                    key={i}
                    className="flex items-center gap-3 border-b border-border/60 px-4 py-3 last:border-b-0"
                  >
                    <div className="h-3 w-1/4 animate-pulse rounded bg-muted" />
                    <div className="h-3 w-1/3 animate-pulse rounded bg-muted/70" />
                    <div className="ml-auto h-3 w-16 animate-pulse rounded bg-muted/70" />
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
        <span className="sr-only">Loading admin console</span>
      </div>
    );
  }

  if (user.role !== "admin") {
    return (
      <div className="flex min-h-screen items-center justify-center gap-2 text-muted-foreground">
        <ShieldAlert className="h-4 w-4" />
        Redirecting…
      </div>
    );
  }

  if (verifying || !adminVerified) {
    return (
      <div className="flex min-h-screen items-center justify-center text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        Verifying admin access…
      </div>
    );
  }

  return (
    <AdminShell
      activeTab={activeTab}
      setActiveTab={setActiveTab}
      frozen={frozen}
    >
      {activeTab === "overview" && (
        <OverviewTab onFrozenChange={setFrozen} />
      )}
      {activeTab === "users" && <UsersTab />}
      {activeTab === "tasks" && <TasksTab />}
      {activeTab === "usage" && <UsageTab />}
      {activeTab === "models" && <ModelsTab />}
      {activeTab === "flags" && <FlagsTab />}
      {activeTab === "audit" && <AuditTab />}
      {activeTab === "health" && <HealthTab />}
      {activeTab === "todo" && <TodoTab />}
      {activeTab === "danger" && (
        <DangerTab frozen={frozen} onFrozenChange={setFrozen} />
      )}
    </AdminShell>
  );
}
