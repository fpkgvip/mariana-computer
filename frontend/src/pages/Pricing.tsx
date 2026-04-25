import { useState } from "react";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, Check, Loader2, Globe, ShieldCheck, Sparkles } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { supabase } from "@/lib/supabase";
import { toast } from "sonner";
import { track } from "@/lib/analytics";
import { BRAND } from "@/lib/brand";

const API_URL = import.meta.env.VITE_API_URL ?? "";

/**
 * Pricing — restated around the "generation is free, shipping is paid"
 * narrative that defines Deft. Plan IDs ("starter", "pro", "max") match
 * the backend Stripe price catalogue and remain stable; only the labels
 * and copy have been rewritten.
 */

interface Plan {
  id: string;
  name: string;
  tagline: string;
  price: number;
  credits: number;
  deploys: string;
  domain: string;
  collab: string;
  features: string[];
  highlighted?: boolean;
  cta: string;
}

const plans: Plan[] = [
  {
    id: "starter",
    name: "Builder",
    tagline: "For weekend hackers and solo founders.",
    price: 20,
    credits: 2000,
    deploys: "20 deploys / month",
    domain: "preview.deft.computer subdomain",
    collab: "Solo workspace",
    cta: "Start building",
    features: [
      "Unlimited generation, planning & verification",
      "20 deploys / month to live URLs",
      "All built-in skills & flagship models",
      "Vault for encrypted secrets",
      "Persistent project history",
      "Community support",
    ],
  },
  {
    id: "pro",
    name: "Pro",
    tagline: "For people shipping every week.",
    price: 50,
    credits: 5500,
    deploys: "Unlimited deploys",
    domain: "Custom domain on any project",
    collab: "Up to 3 collaborators",
    highlighted: true,
    cta: "Go pro",
    features: [
      "Everything in Builder",
      "Unlimited deploys to live URLs",
      "Custom domain (yourapp.com) per project",
      "Up to 3 collaborators per workspace",
      "PDF · DOCX · PPTX · XLSX export",
      "Priority queue & priority support",
    ],
  },
  {
    id: "max",
    name: "Max",
    tagline: "For studios shipping every day.",
    price: 200,
    credits: 25000,
    deploys: "Unlimited deploys",
    domain: "Unlimited custom domains",
    collab: "Unlimited collaborators",
    cta: "Go max",
    features: [
      "Everything in Pro",
      "4 concurrent runs, dedicated queue",
      "Unlimited custom domains",
      "Unlimited collaborators",
      "Image + video generation",
      "Priority support with SLA",
    ],
  },
];

const faqs = [
  {
    q: "What does \"generation is free\" actually mean?",
    a: "Plans include a generous monthly credit budget. Credits are consumed when Deft does work that costs us money — primarily flagship-model tokens and the deploy itself. Planning, light editing, and the verify loop are intentionally cheap, so you never feel the meter while iterating. The only thing that decisively spends credits is shipping a live URL.",
  },
  {
    q: "What is a \"deploy\"?",
    a: "A deploy is a publish to a live URL. Each Builder plan deploy gives you a unique preview.deft.computer subdomain. On Pro and Max you can attach a custom domain to any project.",
  },
  {
    q: "Can I still iterate on a deployed app for free?",
    a: "Yes. Every redeploy of the same project counts as 1 deploy. On Pro and Max, deploys are unlimited — so iteration is fully free.",
  },
  {
    q: "Can I upgrade or downgrade?",
    a: "Yes — changes take effect at your next billing cycle. The billing portal lives in your Account page.",
  },
  {
    q: "What happens if I run out of credits mid-run?",
    a: "Runs stop at the credit ceiling you set up-front. Above the plan budget, you can either top up instantly or wait for the next monthly refresh — in-flight runs always finish.",
  },
  {
    q: "Can I bring my own keys?",
    a: "Yes. The Vault page lets you store API keys (OpenAI, Anthropic, Stripe, Supabase, etc.) encrypted on-device. Reference them in any prompt with $KEY_NAME and Deft injects them at runtime — keys never touch the database in plaintext.",
  },
];

const topups = [
  { id: "topup_starter", name: "Builder pack", price: 10, credits: 1000 },
  { id: "topup_pro", name: "Pro pack", price: 30, credits: 3000 },
  { id: "topup_max", name: "Max pack", price: 150, credits: 15000 },
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

    try {
      const isTopup = planId.startsWith("topup_");
      track("checkout_started", { plan_id: planId, kind: isTopup ? "topup" : "subscription" });
    } catch {
      /* ignore */
    }

    // BUG-FE-121: open synchronously to keep Safari's user gesture alive.
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
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          plan_id: planId,
          success_url: `${window.location.origin}/build?checkout=success`,
          cancel_url: `${window.location.origin}/pricing?checkout=cancelled`,
        }),
      });

      if (!res.ok) {
        const errText = await res.text().catch(() => res.statusText);
        throw new Error(`HTTP ${res.status}: ${errText}`);
      }

      const data: { checkout_url: string; session_id: string } = await res.json();
      if (!data.checkout_url) throw new Error("No checkout URL received from server");
      if (popup) popup.location.href = data.checkout_url;
      else window.location.href = data.checkout_url;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      toast.error("Could not start checkout", { description: msg });
      setLoadingPlanId(null);
    }
  };

  return (
    <div className="relative min-h-screen overflow-hidden bg-background text-foreground">
      <Navbar />

      {/* Hero */}
      <section className="relative isolate pt-32 pb-12 md:pt-40">
        <div className="absolute inset-0 -z-10 bg-grid opacity-50" aria-hidden />
        <div className="absolute inset-0 -z-10 bg-vignette" aria-hidden />
        <div
          className="absolute left-1/2 top-[35%] -z-10 h-[500px] w-[820px] -translate-x-1/2 -translate-y-1/2 rounded-full opacity-20 blur-3xl"
          style={{ background: "radial-gradient(closest-side, hsl(var(--deploy)/0.6), transparent)" }}
          aria-hidden
        />

        <div className="container-deft text-center">
          <div className="mx-auto mb-5 inline-flex items-center gap-2 rounded-full border border-border/70 bg-surface-1/70 px-3 py-1 text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground backdrop-blur">
            <span className="size-1.5 rounded-full bg-deploy animate-pulse" />
            Pricing
          </div>
          <h1 className="mx-auto max-w-3xl text-balance text-[40px] font-semibold leading-[1.05] tracking-[-0.025em] md:text-[64px]">
            Generation is free.
            <br />
            <span className="text-deploy">Pay only when you ship.</span>
          </h1>
          <p className="mx-auto mt-6 max-w-xl text-[15.5px] leading-[1.65] text-muted-foreground">
            Plan, write, build, and verify as much as you want. Credits are only spent when{" "}
            {BRAND.name} pushes your work to a live URL — because that's the only thing that costs us.
          </p>
        </div>
      </section>

      {/* Plans */}
      <section className="relative pb-24">
        <div className="container-deft">
          <div className="mx-auto grid max-w-6xl gap-5 md:grid-cols-3">
            {plans.map((plan) => (
              <div
                key={plan.id}
                className={[
                  "relative flex h-full flex-col rounded-2xl border bg-surface-1 p-7 transition-all",
                  plan.highlighted
                    ? "border-accent/50 shadow-[0_0_0_1px_hsl(var(--accent)/0.15),0_30px_80px_-30px_hsl(var(--accent)/0.45)]"
                    : "border-border/70 shadow-elev-1 hover:border-border",
                ].join(" ")}
              >
                {plan.highlighted && (
                  <div className="absolute -top-3 left-7 inline-flex items-center gap-1.5 rounded-full border border-accent/40 bg-background px-2.5 py-0.5 text-[10.5px] font-medium uppercase tracking-[0.14em] text-accent">
                    <Sparkles size={10} />
                    Most popular
                  </div>
                )}

                <div>
                  <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
                    {plan.name}
                  </p>
                  <p className="mt-1 text-[13px] text-muted-foreground">{plan.tagline}</p>
                </div>

                <div className="mt-6 flex items-baseline gap-1">
                  <span className="text-[44px] font-semibold tracking-[-0.02em] text-foreground">
                    ${plan.price}
                  </span>
                  <span className="text-[13px] text-muted-foreground">/ month</span>
                </div>

                <div className="mt-1 flex items-center gap-1.5 text-[12px] text-muted-foreground">
                  <span className="font-mono text-foreground">{plan.credits.toLocaleString()}</span>
                  <span>credits / month</span>
                </div>

                {/* Magic-three differentiators */}
                <div className="mt-6 grid grid-cols-3 gap-2 rounded-lg border border-border/60 bg-background/50 p-3 text-center">
                  <Stat icon={<Sparkles size={11} className="text-deploy" />} label={plan.deploys.split(" ")[0]} caption="deploys" />
                  <Stat icon={<Globe size={11} className="text-accent" />} label={plan.id === "starter" ? "Subdomain" : "Custom"} caption="domains" />
                  <Stat icon={<ShieldCheck size={11} className="text-foreground/80" />} label={plan.collab.split(" ")[0]} caption="seats" />
                </div>

                <ul className="mt-6 flex-1 space-y-2.5">
                  {plan.features.map((f) => (
                    <li key={f} className="flex items-start gap-2.5 text-[13.5px] text-foreground">
                      <Check size={13} className="mt-1 shrink-0 text-deploy" strokeWidth={2.5} />
                      <span>{f}</span>
                    </li>
                  ))}
                </ul>

                <button
                  type="button"
                  onClick={() => handleSubscribe(plan.id)}
                  disabled={loadingPlanId !== null}
                  className={[
                    "mt-7 flex w-full items-center justify-center gap-1.5 rounded-md px-4 py-2.5 text-[13.5px] font-medium transition-all disabled:opacity-60",
                    plan.highlighted
                      ? "bg-accent text-accent-foreground shadow-[0_4px_16px_-6px_hsl(var(--accent)/0.6)] hover:brightness-110"
                      : "border border-border/70 bg-surface-2 text-foreground hover:bg-surface-3",
                  ].join(" ")}
                >
                  {loadingPlanId === plan.id ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    <>
                      {plan.cta} <ArrowRight size={13} />
                    </>
                  )}
                </button>
              </div>
            ))}
          </div>

          <p className="mt-8 text-center text-[13px] text-muted-foreground">
            Building something at company scale?{" "}
            <Link to="/contact" className="text-foreground underline-offset-4 hover:underline">
              Talk to us
            </Link>{" "}
            about Enterprise.
          </p>
        </div>
      </section>

      {/* Top-ups */}
      <section className="relative border-t border-border/60 bg-surface-1/30 py-20">
        <div className="container-deft mx-auto max-w-5xl">
          <div className="mb-10 flex flex-col gap-2">
            <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-accent">Top-ups</p>
            <h2 className="text-[28px] font-semibold leading-[1.1] tracking-[-0.02em] md:text-[36px]">
              Need more credits this month?
            </h2>
            <p className="max-w-xl text-[14.5px] leading-[1.65] text-muted-foreground">
              Top-ups apply instantly and don't expire while your subscription is active.
            </p>
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            {topups.map((tu) => (
              <div
                key={tu.id}
                className="flex flex-col rounded-xl border border-border/70 bg-surface-1 p-6 transition-colors hover:border-border"
              >
                <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
                  {tu.name}
                </p>
                <h3 className="mt-3 text-[32px] font-semibold tracking-[-0.02em] text-foreground">
                  ${tu.price}
                </h3>
                <p className="mt-1 text-[13px] text-muted-foreground">
                  +{tu.credits.toLocaleString()} credits
                </p>
                <button
                  type="button"
                  onClick={() => handleSubscribe(tu.id)}
                  disabled={loadingPlanId !== null}
                  className="mt-6 flex w-full items-center justify-center gap-2 rounded-md border border-border/70 bg-surface-2 px-4 py-2.5 text-[13px] font-medium text-foreground transition-colors hover:bg-surface-3 disabled:opacity-60"
                >
                  {loadingPlanId === tu.id ? <Loader2 size={14} className="animate-spin" /> : <>Buy now <ArrowRight size={13} /></>}
                </button>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* FAQ */}
      <section className="relative py-24">
        <div className="container-deft mx-auto max-w-3xl">
          <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-accent">FAQ</p>
          <h2 className="mt-3 text-[28px] font-semibold leading-[1.1] tracking-[-0.02em] md:text-[36px]">
            Common questions
          </h2>

          <div className="mt-10 divide-y divide-border/60 border-y border-border/60">
            {faqs.map((faq) => (
              <details key={faq.q} className="group py-5">
                <summary className="flex cursor-pointer list-none items-start justify-between gap-4 text-[15px] font-medium text-foreground">
                  <span>{faq.q}</span>
                  <span className="mt-1 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-md border border-border/60 text-muted-foreground transition-transform group-open:rotate-45">+</span>
                </summary>
                <p className="mt-3 max-w-prose text-[13.5px] leading-[1.7] text-muted-foreground">
                  {faq.a}
                </p>
              </details>
            ))}
          </div>
        </div>
      </section>

      <Footer />
    </div>
  );
}

function Stat({ icon, label, caption }: { icon: React.ReactNode; label: string; caption: string }) {
  return (
    <div className="flex flex-col items-center gap-1">
      <div className="flex items-center gap-1 text-[11.5px] font-medium text-foreground">
        {icon}
        <span className="capitalize">{label}</span>
      </div>
      <span className="text-[10px] uppercase tracking-[0.12em] text-muted-foreground">{caption}</span>
    </div>
  );
}
