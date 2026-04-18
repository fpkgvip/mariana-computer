import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/contexts/AuthContext";
import { LogOut, CreditCard, ShieldCheck, ExternalLink, Loader2 } from "lucide-react";
import { supabase } from "@/lib/supabase";
import { toast } from "sonner";

const API_URL = import.meta.env.VITE_API_URL ?? "";

/** Format a subscription plan slug into a display name */
function formatPlanName(plan: string): string {
  if (!plan || plan === "none") return "No plan";
  return plan.charAt(0).toUpperCase() + plan.slice(1);
}

/** Format subscription status into a readable badge */
function formatStatus(status: string): { label: string; className: string } {
  switch (status) {
    case "active":
      return { label: "Active", className: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 ring-emerald-500/20" };
    case "canceled":
      return { label: "Canceled", className: "bg-red-500/15 text-red-600 dark:text-red-400 ring-red-500/20" };
    case "past_due":
      return { label: "Past due", className: "bg-amber-500/15 text-amber-600 dark:text-amber-400 ring-amber-500/20" };
    case "trialing":
      return { label: "Trialing", className: "bg-blue-500/15 text-blue-600 dark:text-blue-400 ring-blue-500/20" };
    default:
      return { label: "None", className: "bg-zinc-500/15 text-zinc-600 dark:text-zinc-400 ring-zinc-500/20" };
  }
}

export default function Account() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [isOpeningPortal, setIsOpeningPortal] = useState(false);

  // BUG-R1-10: Add a 500ms grace period before redirecting, matching Chat.tsx.
  useEffect(() => {
    if (!user) {
      const timer = setTimeout(() => navigate("/login", { replace: true }), 500);
      return () => clearTimeout(timer);
    }
  }, [user, navigate]);

  if (!user) return null;

  const handleLogout = async () => {
    await logout();
    navigate("/");
  };

  const handleManageSubscription = async () => {
    setIsOpeningPortal(true);
    try {
      const { data: { session } } = await supabase.auth.getSession();
      const token = session?.access_token;
      if (!token) {
        toast.error("Not authenticated", { description: "Please sign in first." });
        navigate("/login");
        return;
      }

      const res = await fetch(`${API_URL}/api/billing/portal`, {
        headers: { Authorization: `Bearer ${token}` },
      });

      if (!res.ok) {
        const errText = await res.text().catch(() => res.statusText);
        throw new Error(`HTTP ${res.status}: ${errText}`);
      }

      const data: { portal_url: string } = await res.json();
      if (!data.portal_url) {
        throw new Error("No portal URL received from server");
      }
      window.location.href = data.portal_url;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      toast.error("Could not open billing portal", { description: msg });
      setIsOpeningPortal(false);
    }
  };

  const statusBadge = formatStatus(user.subscription_status);

  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
        <div className="mx-auto max-w-lg">
          <ScrollReveal>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-bold text-foreground sm:text-3xl">
                Account
              </h1>
              {user.role === "admin" && (
                <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-bold text-primary ring-1 ring-primary/20">
                  <ShieldCheck size={11} />
                  Admin
                </span>
              )}
            </div>
          </ScrollReveal>

          <ScrollReveal>
            <div className="mt-8 rounded-xl border border-border bg-card p-6">
              <div className="space-y-5">
                <div>
                  <p className="text-xs font-bold uppercase tracking-widest text-muted-foreground">Name</p>
                  <p className="mt-1 text-sm text-foreground">{user.name}</p>
                </div>
                <div>
                  <p className="text-xs font-bold uppercase tracking-widest text-muted-foreground">Email</p>
                  <p className="mt-1 text-sm text-foreground">{user.email}</p>
                </div>
                <div>
                  <p className="text-xs font-bold uppercase tracking-widest text-muted-foreground">Plan</p>
                  <div className="mt-1 flex items-center gap-2">
                    <p className="text-sm font-bold text-foreground">
                      {formatPlanName(user.subscription_plan)}
                    </p>
                    {user.subscription_status !== "none" && (
                      <span
                        className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-bold ring-1 ring-inset ${statusBadge.className}`}
                      >
                        {statusBadge.label}
                      </span>
                    )}
                  </div>
                </div>
                <div>
                  <p className="text-xs font-bold uppercase tracking-widest text-muted-foreground">Credits</p>
                  <p className="mt-1 text-lg font-bold text-foreground">
                    {user.tokens.toLocaleString()}
                    <span className="ml-2 text-xs font-normal text-muted-foreground">credits remaining</span>
                  </p>
                </div>
              </div>
            </div>
          </ScrollReveal>

          <ScrollReveal>
            <div className="mt-6 grid gap-3 sm:grid-cols-2">
              <Button
                variant="outline"
                onClick={handleManageSubscription}
                disabled={isOpeningPortal}
                className="w-full justify-start gap-2"
              >
                {isOpeningPortal ? (
                  <Loader2 size={16} className="animate-spin" />
                ) : (
                  <CreditCard size={16} />
                )}
                Manage subscription
                {!isOpeningPortal && <ExternalLink size={12} className="ml-auto opacity-50" />}
              </Button>

              {user.role === "admin" && (
                <Button
                  variant="outline"
                  onClick={() => navigate("/admin")}
                  className="w-full justify-start gap-2"
                >
                  <ShieldCheck size={16} /> Admin panel
                </Button>
              )}
            </div>

            <Button
              variant="ghost"
              onClick={handleLogout}
              className="mt-6 w-full justify-start gap-2 text-muted-foreground"
            >
              <LogOut size={16} /> Sign out
            </Button>
          </ScrollReveal>
        </div>
      </section>

      <Footer />
    </div>
  );
}
