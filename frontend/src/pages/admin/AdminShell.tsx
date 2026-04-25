import { ReactNode } from "react";
import { ShieldCheck } from "lucide-react";
import { Link } from "react-router-dom";

/**
 * Shared shell for every admin tab: branded header + tab nav + content slot.
 * Stateless — the active tab is controlled by the parent.
 */
export function AdminShell({
  activeTab,
  setActiveTab,
  children,
  frozen,
}: {
  activeTab: string;
  setActiveTab: (t: string) => void;
  children: ReactNode;
  frozen?: boolean;
}) {
  const tabs: { key: string; label: string }[] = [
    { key: "overview", label: "Overview" },
    { key: "users", label: "Users" },
    { key: "tasks", label: "Tasks" },
    { key: "usage", label: "Usage & Costs" },
    { key: "models", label: "Models" },
    { key: "flags", label: "Feature Flags" },
    { key: "audit", label: "Audit Log" },
    { key: "health", label: "System Health" },
    { key: "todo", label: "Admin Todo" },
    { key: "danger", label: "Danger Zone" },
  ];

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-[1400px] items-center justify-between px-6 py-4">
          <div className="flex items-center gap-3">
            <ShieldCheck className="h-6 w-6 text-primary" />
            <div>
              <h1 className="font-serif text-xl font-semibold">
                Deft Admin Console
              </h1>
              <p className="text-xs text-muted-foreground">
                v3.7 · full control plane
              </p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            {frozen && (
              <span className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-1 text-xs font-medium uppercase tracking-wider text-destructive">
                System frozen
              </span>
            )}
            <Link
              to="/chat"
              className="text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
            >
              Back to app
            </Link>
          </div>
        </div>
        <nav className="mx-auto max-w-[1400px] overflow-x-auto px-6">
          <ul className="flex gap-1 pb-2">
            {tabs.map((t) => (
              <li key={t.key}>
                <button
                  onClick={() => setActiveTab(t.key)}
                  className={`whitespace-nowrap rounded-t-md px-4 py-2 text-sm font-medium transition-colors ${
                    activeTab === t.key
                      ? "border-b-2 border-primary bg-background text-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {t.label}
                </button>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <main className="mx-auto max-w-[1400px] px-6 py-8">{children}</main>
    </div>
  );
}

export function StatCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string | number;
  sub?: string;
  accent?: "default" | "warning" | "danger" | "success";
}) {
  const accentClass =
    accent === "danger"
      ? "text-destructive"
      : accent === "warning"
      ? "text-amber-500"
      : accent === "success"
      ? "text-emerald-500"
      : "text-foreground";
  return (
    <div className="rounded-lg border border-border bg-card p-5">
      <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </p>
      <p className={`mt-2 font-serif text-2xl font-semibold ${accentClass}`}>
        {value}
      </p>
      {sub && <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}

export function SectionHeader({
  title,
  action,
}: {
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-4 flex items-center justify-between">
      <h2 className="font-serif text-lg font-semibold">{title}</h2>
      {action}
    </div>
  );
}
