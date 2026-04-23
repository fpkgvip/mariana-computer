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
      navigate("/chat", { replace: true });
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
        navigate("/chat", { replace: true });
      } finally {
        if (!cancelled) setVerifying(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [user, navigate]);

  if (!user) {
    return (
      <div className="flex min-h-screen items-center justify-center text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        Loading…
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
