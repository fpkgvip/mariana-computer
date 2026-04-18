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
  price_usd: number;
  credits_per_month: number;
  features: string[] | null;
  sort_order: number;
  is_public: boolean;
  coming_soon?: boolean;
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
        .select("id, name, price_usd, credits_per_month, features, sort_order, is_public, coming_soon")
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
            <h1 className="text-2xl font-bold text-foreground sm:text-3xl">
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
              <div className="mt-12 rounded-xl border border-border bg-card p-8 text-center">
                <p className="text-sm text-muted-foreground">
                  No plans available at the moment. Please check back later or{" "}
                  <a href="/contact" className="font-semibold text-primary hover:underline">
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
                  <div className="flex h-full flex-col rounded-xl border border-border bg-card p-6 shadow-sm transition-all hover:shadow-md">
                    <p className="text-xs font-bold uppercase tracking-widest text-muted-foreground">
                      {plan.name}
                    </p>
                    <h2 className="mt-3 text-3xl font-bold text-foreground">
                      ${plan.price_usd.toLocaleString()}
                      <span className="text-base font-normal text-muted-foreground">/month</span>
                    </h2>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {plan.credits_per_month.toLocaleString()} credits/month
                    </p>

                    <div className="my-5 border-t border-border" />

                    {plan.features && plan.features.length > 0 && (
                      <ul className="flex-1 space-y-2.5">
                        {plan.features.map((feature) => (
                          <li
                            key={feature}
                            className="flex items-start gap-2.5 text-sm text-foreground"
                          >
                            <Check
                              size={14}
                              className="mt-0.5 shrink-0 text-primary"
                              strokeWidth={2.5}
                            />
                            {feature}
                          </li>
                        ))}
                      </ul>
                    )}

                    {plan.coming_soon ? (
                      <div className="mt-6 flex w-full items-center justify-center gap-2 rounded-lg border border-border px-4 py-2.5 text-sm font-semibold text-muted-foreground cursor-not-allowed">
                        Coming Soon
                      </div>
                    ) : (
                      <button
                        onClick={() => handleSubscribe(plan.id)}
                        disabled={loadingPlanId !== null}
                        className="mt-6 flex w-full items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-semibold text-primary-foreground shadow-md transition-all hover:opacity-90 hover:shadow-lg disabled:opacity-60"
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
