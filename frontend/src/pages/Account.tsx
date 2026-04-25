/**
 * Account — production route at /account.
 *
 * Renders AccountView with real user data from AuthContext + /api/billing/*.
 * Bucket detail and ledger transactions degrade calmly when the backend
 * hasn't yet exposed them: the balance card still renders with totals, and
 * the activity card shows the empty state instead of fabricating rows.
 */
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { useAuth } from "@/contexts/AuthContext";
import { supabase } from "@/lib/supabase";
import { toast } from "sonner";
import {
  AccountView,
  type AccountData,
  type CreditBucket,
  type LedgerTx,
  type SubscriptionStatus,
} from "@/components/deft/account/AccountView";
import { BuyCreditsDialog } from "@/components/deft/account/BuyCreditsDialog";

const API_URL = import.meta.env.VITE_API_URL ?? "";

const PLAN_LIBRARY: Record<
  string,
  { name: string; price_usd_monthly: number; credits_per_month: number }
> = {
  none: { name: "Free", price_usd_monthly: 0, credits_per_month: 500 },
  free: { name: "Free", price_usd_monthly: 0, credits_per_month: 500 },
  starter: { name: "Starter", price_usd_monthly: 29, credits_per_month: 3_500 },
  standard: { name: "Standard", price_usd_monthly: 99, credits_per_month: 13_000 },
  pro: { name: "Pro", price_usd_monthly: 299, credits_per_month: 42_000 },
  scale: { name: "Scale", price_usd_monthly: 699, credits_per_month: 100_000 },
  // legacy slugs
  researcher: { name: "Starter", price_usd_monthly: 29, credits_per_month: 3_500 },
  professional: { name: "Standard", price_usd_monthly: 99, credits_per_month: 13_000 },
  enterprise: { name: "Pro", price_usd_monthly: 299, credits_per_month: 42_000 },
};

function normalizePlan(slug: string | null | undefined) {
  const key = (slug ?? "none").toLowerCase();
  const meta = PLAN_LIBRARY[key] ?? PLAN_LIBRARY.none;
  return { id: key, ...meta };
}

function normalizeStatus(s: string | null | undefined): SubscriptionStatus {
  switch ((s ?? "").toLowerCase()) {
    case "active":
    case "canceled":
    case "past_due":
    case "trialing":
      return s as SubscriptionStatus;
    default:
      return "none";
  }
}

interface UsageResponse {
  plan: { id: string; name: string; price_usd_monthly: number; credits_per_month: number };
  subscription_status: string;
  credits_remaining: number | null;
  credits_used_this_period: number;
  credits_used_pct: number;
  next_renewal_at?: string | null;
  buckets?: CreditBucket[];
  transactions?: LedgerTx[];
}

export default function Account() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [isOpeningPortal, setIsOpeningPortal] = useState(false);
  const [isBuyOpen, setIsBuyOpen] = useState(false);
  const [isPurchasing, setIsPurchasing] = useState(false);
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const portalAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!user) {
      const timer = setTimeout(() => navigate("/login", { replace: true }), 500);
      return () => clearTimeout(timer);
    }
  }, [user, navigate]);

  useEffect(() => {
    return () => {
      portalAbortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const {
          data: { session },
        } = await supabase.auth.getSession();
        const token = session?.access_token;
        if (!token) return;
        const res = await fetch(`${API_URL}/api/billing/usage`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) return;
        const data = (await res.json()) as UsageResponse;
        if (!cancelled) setUsage(data);
      } catch {
        // calm degrade — totals from auth context still render
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!user) {
    return (
      <div className="min-h-screen bg-background">
        <Navbar />
        <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
          <div className="mx-auto max-w-3xl">
            <div className="h-8 w-32 animate-pulse rounded bg-muted" />
            <div className="mt-8 grid gap-5 md:grid-cols-2">
              <div className="h-56 animate-pulse rounded-xl border border-border/60 bg-card/50" />
              <div className="h-56 animate-pulse rounded-xl border border-border/60 bg-card/50" />
            </div>
            <div className="mt-5 h-72 animate-pulse rounded-xl border border-border/60 bg-card/50" />
          </div>
        </section>
        <Footer />
      </div>
    );
  }

  const handleLogout = async () => {
    await logout();
    navigate("/");
  };

  const handleBuyCredits = async (packId: string) => {
    setIsPurchasing(true);
    // Open synchronously to keep Safari's user gesture alive (BUG-FE-121).
    const popup = window.open("", "_self");
    try {
      const {
        data: { session },
      } = await supabase.auth.getSession();
      const token = session?.access_token;
      if (!token) {
        toast.error("Not authenticated", { description: "Sign in and try again." });
        navigate("/login");
        return;
      }
      const res = await fetch(`${API_URL}/api/billing/create-checkout`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          plan_id: packId,
          success_url: `${window.location.origin}/account?topup=success`,
          cancel_url: `${window.location.origin}/account?topup=cancelled`,
        }),
      });
      if (!res.ok) {
        const errText = await res.text().catch(() => res.statusText);
        throw new Error(`HTTP ${res.status}: ${errText}`);
      }
      const data: { checkout_url?: string } = await res.json();
      if (!data.checkout_url) throw new Error("No checkout URL received from server");
      // Validate origin: same-origin or stripe.com only.
      const parsed = new URL(data.checkout_url);
      const isSameOrigin = parsed.origin === window.location.origin;
      const isStripe = parsed.hostname.endsWith(".stripe.com");
      if (!isSameOrigin && !isStripe) {
        throw new Error("Untrusted checkout URL");
      }
      if (popup) popup.location.href = data.checkout_url;
      else window.location.href = data.checkout_url;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      toast.error("Could not start checkout", { description: msg });
      setIsPurchasing(false);
    }
  };

  const handleManageSubscription = async () => {
    setIsOpeningPortal(true);
    const popup = window.open("", "_self");
    const ac = new AbortController();
    portalAbortRef.current?.abort();
    portalAbortRef.current = ac;

    try {
      const {
        data: { session },
      } = await supabase.auth.getSession();
      const token = session?.access_token;
      if (!token) {
        toast.error("Not authenticated", { description: "Sign in and try again." });
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
      if (!data.portal_url) throw new Error("No portal URL received from server");
      if (popup) popup.location.href = data.portal_url;
      else window.location.href = data.portal_url;
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      const msg = err instanceof Error ? err.message : "Unknown error";
      toast.error("Could not open billing portal", { description: msg });
      setIsOpeningPortal(false);
    }
  };

  const plan = normalizePlan(usage?.plan?.id ?? user.subscription_plan);
  const status = normalizeStatus(usage?.subscription_status ?? user.subscription_status);

  const data: AccountData = {
    name: user.name,
    email: user.email,
    role: user.role === "admin" ? "admin" : "user",
    plan,
    subscriptionStatus: status,
    nextRenewal: usage?.next_renewal_at ?? null,
    balance: usage?.credits_remaining ?? user.tokens ?? 0,
    buckets: usage?.buckets ?? [],
    transactions: usage?.transactions ?? [],
  };

  return (
    <div className="flex min-h-screen flex-col bg-background">
      <Navbar />
      <main className="flex-1 pt-20">
        <AccountView
          data={data}
          isOpeningPortal={isOpeningPortal}
          onManageBilling={handleManageSubscription}
          onAddCredits={() => setIsBuyOpen(true)}
          onLogout={handleLogout}
          onNavigateAdmin={
            user.role === "admin" ? () => navigate("/admin") : undefined
          }
        />
        <BuyCreditsDialog
          open={isBuyOpen}
          onOpenChange={(o) => {
            if (!isPurchasing) setIsBuyOpen(o);
          }}
          onPurchase={handleBuyCredits}
          isPurchasing={isPurchasing}
          currentBalance={data.balance}
        />
      </main>
      <Footer />
    </div>
  );
}
