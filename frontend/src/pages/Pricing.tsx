import { useState } from "react";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, Check, Loader2 } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { supabase } from "@/lib/supabase";
import { toast } from "sonner";

const API_URL = import.meta.env.VITE_API_URL ?? "";

interface Plan {
  id: string;
  name: string;
  price: number;
  credits: number;
  features: string[];
  highlighted?: boolean;
  comingSoon?: boolean;
}

const plans: Plan[] = [
  {
    id: "individual",
    name: "Individual",
    price: 299,
    credits: 30000,
    highlighted: true,
    features: [
      "30,000 credits/month",
      "Full access to Mariana Computer",
      "Instant, standard, and deep investigations",
      "All built-in research skills",
      "PDF report downloads",
      "File uploads and attachments",
      "Priority support",
    ],
  },
  {
    id: "enterprise",
    name: "Enterprise",
    price: 3999,
    credits: 500000,
    comingSoon: true,
    features: [
      "500,000 credits/month",
      "Everything in Individual",
      "Custom skills and workflows",
      "Dedicated account manager",
      "SLA-backed support",
      "Custom data source integration",
      "White-glove onboarding",
    ],
  },
  {
    id: "custom",
    name: "Custom",
    price: 0,
    credits: 0,
    comingSoon: true,
    features: [
      "Unlimited credits",
      "Everything in Enterprise",
      "On-premise deployment options",
      "Custom model fine-tuning",
      "Dedicated infrastructure",
      "Bespoke data pipeline integration",
      "24/7 priority engineering support",
    ],
  },
];

const faqs = [
  {
    q: "What are credits?",
    a: "Credits are the unit of research capacity. Each investigation consumes credits based on complexity, data sources accessed, and compute used.",
  },
  {
    q: "How does billing work?",
    a: "Choose a plan that fits your research volume. Credits refresh monthly. Unused credits do not roll over.",
  },
  {
    q: "Can I upgrade or downgrade?",
    a: "Yes, changes take effect at your next billing cycle. Use the billing portal in your account settings.",
  },
  {
    q: "What happens if I run out of credits?",
    a: "Your investigations will pause until your credits refresh or you upgrade your plan.",
  },
  {
    q: "What data sources does Mariana access?",
    a: "SEC EDGAR, earnings transcripts, corporate registries, academic databases, and select financial data providers. Enterprise clients can connect their own proprietary data feeds and terminal credentials.",
  },
];

const howItWorks = [
  {
    step: "01",
    title: "Describe your question",
    desc: "Type anything — from a quick factual question to a complex multi-day investigation. Mariana classifies your request automatically.",
  },
  {
    step: "02",
    title: "Review the plan",
    desc: "For substantial research, Mariana proposes an approach: data sources, methodology, estimated duration. Approve with one click.",
  },
  {
    step: "03",
    title: "Receive results",
    desc: "Mariana works autonomously — reading filings, writing code, building models. You get notified when deliverables are ready.",
  },
];

export default function Pricing() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [loadingPlanId, setLoadingPlanId] = useState<string | null>(null);

  const handleSubscribe = async (planId: string) => {
    if (!user) {
      navigate("/signup");
      return;
    }

    setLoadingPlanId(planId);

    // BUG-FE-121 fix: Open the navigation target synchronously on click so
    // Safari honors the user-gesture context. Assigning window.location.href
    // after an await can be silently blocked in Safari because the gesture
    // has expired by then. We open _self so there's no popup-blocker issue.
    const popup = window.open("", "_self");

    try {
      const { data: { session } } = await supabase.auth.getSession();
      const token = session?.access_token;
      if (!token) {
        toast.error("Not authenticated", { description: "Please sign in first." });
        setLoadingPlanId(null);
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
          cancel_url: `${window.location.origin}/pricing?checkout=cancelled`,
        }),
      });

      if (!res.ok) {
        const errText = await res.text().catch(() => res.statusText);
        throw new Error(`HTTP ${res.status}: ${errText}`);
      }

      const data: { checkout_url: string; session_id: string } = await res.json();
      // P1-FIX-82: Guard against missing checkout_url
      if (!data.checkout_url) {
        throw new Error("No checkout URL received from server");
      }
      // BUG-FE-121: Navigate the pre-opened window (or fall back to current).
      if (popup) {
        popup.location.href = data.checkout_url;
      } else {
        window.location.href = data.checkout_url;
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      toast.error("Could not start checkout", { description: msg });
      setLoadingPlanId(null);
    }
  };

  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <div className="mx-auto max-w-7xl px-6 pb-24 pt-32 md:pt-40">
        <ScrollReveal>
          <h1 className="font-serif text-3xl font-semibold leading-[1.08] tracking-[-0.02em] text-foreground sm:text-4xl md:text-5xl lg:text-[3.5rem]">
            Simple, transparent pricing.
          </h1>
          <p className="mt-6 max-w-lg text-lg leading-[1.7] text-muted-foreground">
            Choose the plan that fits your research volume. Credits refresh monthly.
          </p>
        </ScrollReveal>

        {/* Subscription plans */}
        <div className="mt-12 grid gap-6 sm:mt-16 md:grid-cols-3 max-w-5xl mx-auto">
          {plans.map((plan, i) => (
            <ScrollReveal key={plan.id} delay={i * 100}>
              <div
                className={`relative flex h-full flex-col rounded-lg p-8 shadow-sm ring-1 ${
                  plan.highlighted
                    ? "bg-primary/5 ring-primary/30"
                    : "bg-card ring-border"
                } ${plan.comingSoon ? "opacity-75 pointer-events-none" : ""}`}
                aria-disabled={plan.comingSoon}
              >
                {plan.comingSoon && (
                  <div className="absolute inset-0 z-10 flex items-center justify-center rounded-lg bg-background/60 backdrop-blur-[2px]">
                    <div className="rounded-full border border-accent/40 bg-accent/10 px-5 py-2">
                      <span className="font-serif text-lg font-semibold text-accent">Coming Soon</span>
                    </div>
                  </div>
                )}
                {plan.highlighted && (
                  <p className="mb-3 inline-block self-start rounded-full bg-primary/10 px-3 py-0.5 text-[11px] font-medium text-primary">
                    Most popular
                  </p>
                )}
                <p className="text-xs font-medium uppercase tracking-[0.15em] text-muted-foreground">
                  {plan.name}
                </p>
                <h2 className="mt-4 font-serif text-4xl font-semibold text-foreground">
                  {plan.price > 0 ? (
                    <>${plan.price.toLocaleString()}<span className="text-lg font-normal text-muted-foreground">/month</span></>
                  ) : (
                    <span className="text-2xl">Contact us</span>
                  )}
                </h2>
                <p className="mt-2 text-sm text-muted-foreground">
                  {plan.credits > 0 ? `${plan.credits.toLocaleString()} credits/month` : "Custom volume"}
                </p>

                <div className="my-6 border-t border-border" />

                <ul className="flex-1 space-y-3">
                  {plan.features.map((f) => (
                    <li key={f} className="flex items-start gap-2.5 text-[15px] text-foreground">
                      <Check size={15} className="mt-0.5 shrink-0 text-accent" strokeWidth={2} />
                      {f}
                    </li>
                  ))}
                </ul>

                <div className="mt-8">
                  {plan.comingSoon ? (
                    <div
                      className="flex w-full items-center justify-center gap-2 rounded-md border border-border px-4 py-3 text-sm font-medium text-muted-foreground cursor-not-allowed"
                    >
                      Coming Soon
                    </div>
                  ) : /* This branch activates when comingSoon is removed from Enterprise */ plan.id === "enterprise" ? (
                    <Link
                      to="/contact"
                      className="flex w-full items-center justify-center gap-2 rounded-md border border-border px-4 py-3 text-sm font-medium text-foreground transition-all hover:bg-secondary"
                    >
                      Contact Sales <ArrowRight size={15} />
                    </Link>
                  ) : (
                    <button
                      onClick={() => handleSubscribe(plan.id)}
                      disabled={loadingPlanId !== null}
                      className="flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-3 text-sm font-medium text-primary-foreground transition-all hover:bg-primary/90 disabled:opacity-60"
                    >
                      {loadingPlanId === plan.id ? (
                        <Loader2 size={14} className="animate-spin" />
                      ) : (
                        <>
                          Get Started <ArrowRight size={15} />
                        </>
                      )}
                    </button>
                  )}
                </div>
              </div>
            </ScrollReveal>
          ))}
        </div>

        {/* Not sure? */}
        <ScrollReveal>
          <div className="mt-8 text-center text-sm text-muted-foreground">
            Not sure which plan fits?{" "}
            <Link to="/contact" className="text-foreground underline underline-offset-2 hover:text-accent transition-colors">
              Contact us
            </Link>{" "}
            and we'll help you choose.
          </div>
        </ScrollReveal>

        {/* How it works */}
        <div className="mt-24">
          <ScrollReveal>
            <h2 className="font-serif text-2xl font-semibold text-foreground md:text-3xl">
              How it works
            </h2>
          </ScrollReveal>
          <ScrollReveal delay={100}>
            <div className="mt-8 grid gap-6 sm:grid-cols-3">
              {howItWorks.map((s) => (
                <div key={s.step} className="rounded-lg bg-card p-6 shadow-sm ring-1 ring-border">
                  <span className="font-mono text-xs text-accent">{s.step}</span>
                  <h3 className="mt-2 text-[15px] font-semibold text-foreground">{s.title}</h3>
                  <p className="mt-2 text-sm leading-[1.7] text-muted-foreground">{s.desc}</p>
                </div>
              ))}
            </div>
          </ScrollReveal>
        </div>

        {/* FAQ */}
        <div className="mt-24">
          <ScrollReveal>
            <h2 className="font-serif text-2xl font-semibold text-foreground md:text-3xl">
              Common questions
            </h2>
          </ScrollReveal>
          <div className="mt-8 divide-y divide-border">
            {faqs.map((faq, i) => (
              <ScrollReveal key={faq.q} delay={i * 60}>
                <div className="py-6">
                  <h3 className="text-[15px] font-semibold text-foreground">{faq.q}</h3>
                  <p className="mt-2 text-sm leading-[1.7] text-muted-foreground">
                    {faq.a}
                  </p>
                </div>
              </ScrollReveal>
            ))}
          </div>
        </div>
      </div>

      <Footer />
    </div>
  );
}
