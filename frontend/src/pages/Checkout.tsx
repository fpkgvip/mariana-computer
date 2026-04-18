import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { useAuth } from "@/contexts/AuthContext";
import { supabase } from "@/lib/supabase";
import { toast } from "sonner";
import { ArrowRight, Check, Loader2 } from "lucide-react";

const API_URL = import.meta.env.VITE_API_URL ?? "";

/** Shape of a row from the public.plans table */
interface PlanRow {
  id: string;
  name: string;
  price_monthly: number;
  credits_monthly: number;
  features: Record<string, unknown> | string[] | null;
  sort_order: number;
  is_public: boolean;
  coming_soon: boolean;
}

export default function Checkout() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [plans, setPlans] = useState<PlanRow[]>([]);
  const [loadingPlans, setLoadingPlans] = useState(true);
  const [loadingPlanId, setLoadingPlanId] = useState<string | null>(null);

  // BUG-R1-10: Add a 500ms grace period before redirecting, matching Chat.tsx.
  useEffect(() => {
    if (!user) {
      const timer = setTimeout(() => navigate("/login", { replace: true }), 500);
      return () => clearTimeout(timer);
    }
  }, [user, navigate]);

  // Fetch public plans from Supabase
  useEffect(() => {
    const loadPlans = async () => {
      const { data, error } = await supabase
        .from("plans")
        .select("id, name, price_monthly, credits_monthly, features, sort_order, is_public, coming_soon")
        .eq("is_public", true)
        .order("sort_order");

      if (error) {
        console.error("[Checkout] Failed to load plans:", error.message);
        toast.error("Could not load plans", { description: error.message });
      } else {
        setPlans((data ?? []) as PlanRow[]);
      }
      setLoadingPlans(false);
    };
    loadPlans();
  }, []);

  if (!user) return null;

  const handleSubscribe = async (planId: string) => {
    setLoadingPlanId(planId);

    try {
      const { data: { session } } = await supabase.auth.getSession();
      const token = session?.access_token;
      if (!token) {
        toast.error("Not authenticated", { description: "Please sign in first." });
        navigate("/login");
        return;
      }

      const res = await fetch(`${API_URL}/api/billing/create-checkout`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        // BUG-R2-S2-02: Must send success_url and cancel_url per API contract.
        // Pricing.tsx sends both; Checkout.tsx was missing them, causing the backend
        // to reject the request or use broken default redirect URLs.
        body: JSON.stringify({
          plan_id: planId,
          success_url: `${window.location.origin}/chat?checkout=success`,
          cancel_url: `${window.location.origin}/checkout?checkout=cancelled`,
        }),
      });

      if (!res.ok) {
        const errText = await res.text().catch(() => res.statusText);
        throw new Error(`HTTP ${res.status}: ${errText}`);
      }

      // BUG-R2-S2-01: Backend returns { checkout_url, session_id } — not { url }.
      // Pricing.tsx already used the correct field; Checkout.tsx was using the wrong one,
      // causing a redirect to "undefined" after successful checkout creation.
      const data: { checkout_url: string; session_id: string } = await res.json();
      window.location.href = data.checkout_url;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      toast.error("Could not start checkout", { description: msg });
      setLoadingPlanId(null);
    }
  };

  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
        <div className="mx-auto max-w-4xl">
          <ScrollReveal>
            <h1 className="font-serif text-2xl font-semibold text-foreground sm:text-3xl">
              Choose a plan
            </h1>
            <p className="mt-2 text-sm text-muted-foreground">
              Credits refresh monthly. Select the plan that fits your research volume.
            </p>
          </ScrollReveal>

          {loadingPlans ? (
            <div className="mt-16 flex items-center justify-center">
              <Loader2 size={24} className="animate-spin text-muted-foreground" />
            </div>
          ) : plans.length === 0 ? (
            <ScrollReveal>
              <div className="mt-12 rounded-lg border border-border bg-card p-8 text-center">
                <p className="text-sm text-muted-foreground">
                  No plans available at the moment. Please check back later or{" "}
                  <a href="/contact" className="text-foreground underline underline-offset-2">
                    contact us
                  </a>
                  .
                </p>
              </div>
            </ScrollReveal>
          ) : (
            <div className="mt-10 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
              {plans.map((plan, i) => (
                <ScrollReveal key={plan.id} delay={i * 80}>
                  <div className="flex h-full flex-col rounded-lg bg-card p-6 shadow-sm ring-1 ring-border">
                    <p className="text-xs font-medium uppercase tracking-[0.15em] text-muted-foreground">
                      {plan.name}
                    </p>
                    <h2 className="mt-3 font-serif text-3xl font-semibold text-foreground">
                      ${plan.price_monthly.toLocaleString()}
                      <span className="text-base font-normal text-muted-foreground">/month</span>
                    </h2>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {plan.credits_monthly.toLocaleString()} credits/month
                    </p>

                    <div className="my-5 border-t border-border" />

                    {plan.features && (() => {
                      // Features may be a string[] or a JSONB object — normalise to string[]
                      const featureList: string[] = Array.isArray(plan.features)
                        ? plan.features
                        : Object.entries(plan.features as Record<string, unknown>)
                            .filter(([, v]) => v !== false && v !== null && v !== undefined)
                            .map(([k, v]) => {
                              const label = k.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
                              if (v === true) return label;
                              return `${label}: ${String(v)}`;
                            });
                      return featureList.length > 0 ? (
                        <ul className="flex-1 space-y-2.5">
                          {featureList.map((feature) => (
                            <li
                              key={feature}
                              className="flex items-start gap-2.5 text-[14px] text-foreground"
                            >
                              <Check
                                size={14}
                                className="mt-0.5 shrink-0 text-accent"
                                strokeWidth={2}
                              />
                              {feature}
                            </li>
                          ))}
                        </ul>
                      ) : null;
                    })()}

                    {plan.coming_soon ? (
                      <div className="mt-6 flex w-full items-center justify-center gap-2 rounded-md border border-border px-4 py-2.5 text-sm font-medium text-muted-foreground cursor-not-allowed">
                        Coming Soon
                      </div>
                    ) : (
                      <button
                        onClick={() => handleSubscribe(plan.id)}
                        disabled={loadingPlanId !== null}
                        className="mt-6 flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-all hover:bg-primary/90 disabled:opacity-60"
                      >
                        {loadingPlanId === plan.id ? (
                          <Loader2 size={14} className="animate-spin" />
                        ) : (
                          <>
                            Subscribe <ArrowRight size={14} />
                          </>
                        )}
                      </button>
                    )}
                  </div>
                </ScrollReveal>
              ))}
            </div>
          )}
        </div>
      </section>

      <Footer />
    </div>
  );
}
