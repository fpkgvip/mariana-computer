import { useEffect, useRef, useState } from "react";
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
  if (!plan || plan === "none") return "Free";
  return plan.charAt(0).toUpperCase() + plan.slice(1);
}

/** Format subscription status into a readable badge */
function formatStatus(status: string): { label: string; className: string } {
  switch (status) {
    case "active":
      return { label: "Active", className: "bg-green-500/20 text-green-400 ring-green-500/30" };
    case "canceled":
      return { label: "Canceled", className: "bg-red-500/20 text-red-400 ring-red-500/30" };
    case "past_due":
      return { label: "Past due", className: "bg-amber-500/20 text-amber-400 ring-amber-500/30" };
    case "trialing":
      return { label: "Trialing", className: "bg-blue-500/20 text-blue-400 ring-blue-500/30" };
    default:
      return { label: "None", className: "bg-zinc-500/20 text-zinc-400 ring-zinc-500/30" };
  }
}

export default function Account() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [isOpeningPortal, setIsOpeningPortal] = useState(false);
  // BUG-FE-120 fix: Track the in-flight portal fetch so we can abort on unmount
  // and avoid state updates after the component is gone.
  const portalAbortRef = useRef<AbortController | null>(null);

  // BUG-R1-10: Add a 500ms grace period before redirecting, matching Chat.tsx.
  // Supabase token refresh briefly sets user=null; without the delay, users
  // navigating to this page during a refresh cycle are incorrectly sent to /login.
  useEffect(() => {
    if (!user) {
      const timer = setTimeout(() => navigate("/login", { replace: true }), 500);
      return () => clearTimeout(timer);
    }
  }, [user, navigate]);

  // BUG-FE-120 fix: Abort any in-flight portal fetch on unmount.
  useEffect(() => {
    return () => {
      portalAbortRef.current?.abort();
    };
  }, []);

  // BUG-FE-120 fix: Render a lightweight loading skeleton instead of `return null`
  // during the brief grace-period window. This keeps the page visually stable while
  // Supabase refreshes tokens, and prevents child components from unmounting and
  // losing state (e.g. an in-flight billing-portal fetch).
  if (!user) {
    return (
      <div className="min-h-screen bg-background">
        <Navbar />
        <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
          <div className="mx-auto max-w-lg">
            <div className="h-8 w-32 animate-pulse rounded bg-muted" />
            <div className="mt-8 h-64 animate-pulse rounded-lg border border-border bg-card/50" />
            <div className="mt-6 h-10 animate-pulse rounded-md bg-muted" />
          </div>
        </section>
        <Footer />
      </div>
    );
  }

  // BUG-R2-16: Make async and await logout() so navigation doesn't fire
  // before supabase.auth.signOut() completes and setUser(null) runs.
  // Without await, the user briefly sees the logged-in navbar state after redirect.
  const handleLogout = async () => {
    await logout();
    navigate("/");
  };

  const handleManageSubscription = async () => {
    setIsOpeningPortal(true);

    // BUG-FE-121 fix: Open the window synchronously on click so Safari treats
    // it as a user-gesture navigation. Safari blocks window.open / location
    // assignments issued after an `await` because the gesture context has
    // expired by then.
    const popup = window.open("", "_self");

    // BUG-FE-120 fix: Track the fetch with an AbortController so unmount can cancel it.
    const ac = new AbortController();
    portalAbortRef.current?.abort();
    portalAbortRef.current = ac;

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
        signal: ac.signal,
      });

      if (!res.ok) {
        const errText = await res.text().catch(() => res.statusText);
        throw new Error(`HTTP ${res.status}: ${errText}`);
      }

      const data: { portal_url: string } = await res.json();
      // P1-FIX-82b: Guard against missing portal_url
      if (!data.portal_url) {
        throw new Error("No portal URL received from server");
      }
      // BUG-FE-121: Navigate the pre-opened window (or current if popup is null).
      if (popup) {
        popup.location.href = data.portal_url;
      } else {
        window.location.href = data.portal_url;
      }
    } catch (err) {
      // Don't toast on deliberate abort (e.g. unmount).
      if (err instanceof DOMException && err.name === "AbortError") {
        return;
      }
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
              <h1 className="font-serif text-2xl font-semibold text-foreground sm:text-3xl">
                Account
              </h1>
              {user.role === "admin" && (
                <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary ring-1 ring-primary/20">
                  <ShieldCheck size={11} />
                  Admin
                </span>
              )}
            </div>
          </ScrollReveal>

          <ScrollReveal>
            <div className="mt-8 rounded-lg border border-border bg-card p-6">
              <div className="space-y-4">
                <div>
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Name</p>
                  <p className="mt-1 text-sm text-foreground">{user.name}</p>
                </div>
                <div>
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Email</p>
                  <p className="mt-1 text-sm text-foreground">{user.email}</p>
                </div>
                <div>
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Plan</p>
                  <div className="mt-1 flex items-center gap-2">
                    <p className="text-sm font-semibold text-foreground">
                      {formatPlanName(user.subscription_plan)}
                    </p>
                    {user.subscription_status !== "none" && (
                      <span
                        className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium ring-1 ring-inset ${statusBadge.className}`}
                      >
                        {statusBadge.label}
                      </span>
                    )}
                  </div>
                </div>
                <div>
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Credits</p>
                  <p className="mt-1 text-lg font-semibold text-foreground">
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
