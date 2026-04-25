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
import { usePageHead } from "@/lib/pageHead";

const API_URL = import.meta.env.VITE_API_URL ?? "";

/**
 * Pricing — restated around the locked thesis: "You only pay for software
 * that runs." Plan IDs ("starter", "standard", "pro", "scale") match the
 * Stripe price catalogue locked in the Phase 1 positioning doc and the
 * Account.tsx PLAN_LIBRARY. 1 credit = $0.01 across the product.
 */

interface Plan {
  id: string;
  name: string;
  tagline: string;
  price: number;
  credits: number;
  deploys: string;
  /** Tight one-word stat for the deploys cell (e.g. "20", "Unlimited"). */
  deploysShort: string;
  domain: string;
  /** Tight one-word stat for the domain cell (e.g. "Subdomain", "Custom"). */
  domainShort: string;
  collab: string;
  /** Tight one-word stat for the seats cell (e.g. "1", "3", "10", "Unlimited"). */
  seatsShort: string;
  features: string[];
  highlighted?: boolean;
  cta: string;
}

const plans: Plan[] = [
  {
    id: "starter",
    name: "Starter",
    tagline: "For weekend projects and solo founders.",
    price: 29,
    credits: 3_500,
    deploys: "20 deploys / month",
    domain: "preview.deft.computer subdomain",
    collab: "Solo workspace",
    cta: "Start with Starter",
    deploysShort: "20",
    domainShort: "Subdomain",
    seatsShort: "1",
    features: [
      "Unlimited planning, writing, and verification",
      "20 deploys to live URLs each month",
      "All built-in skills and flagship models",
      "Vault for encrypted API keys",
      "Persistent project history",
      "Community support",
    ],
  },
  {
    id: "standard",
    name: "Standard",
    tagline: "For people deploying every week.",
    price: 99,
    credits: 13_000,
    deploys: "Unlimited deploys",
    domain: "Custom domain on any project",
    collab: "Up to 3 collaborators",
    highlighted: true,
    cta: "Choose Standard",
    deploysShort: "Unlimited",
    domainShort: "Custom",
    seatsShort: "3",
    features: [
      "Everything in Starter",
      "Unlimited deploys to live URLs",
      "Custom domain (yourapp.com) per project",
      "Up to 3 collaborators per workspace",
      "PDF · DOCX · PPTX · XLSX export",
      "Priority queue and priority support",
    ],
  },
  {
    id: "pro",
    name: "Pro",
    tagline: "For teams that publish daily.",
    price: 299,
    credits: 42_000,
    deploys: "Unlimited deploys",
    domain: "Unlimited custom domains",
    collab: "Up to 10 collaborators",
    cta: "Choose Pro",
    deploysShort: "Unlimited",
    domainShort: "Custom",
    seatsShort: "10",
    features: [
      "Everything in Standard",
      "4 concurrent runs, dedicated queue",
      "Unlimited custom domains",
      "Up to 10 collaborators per workspace",
      "Image and video generation",
      "Priority support with one-business-day SLA",
    ],
  },
  {
    id: "scale",
    name: "Scale",
    tagline: "For studios running runs all day.",
    price: 699,
    credits: 100_000,
    deploys: "Unlimited deploys",
    domain: "Unlimited custom domains",
    collab: "Unlimited collaborators",
    cta: "Choose Scale",
    deploysShort: "Unlimited",
    domainShort: "Custom",
    seatsShort: "Unlimited",
    features: [
      "Everything in Pro",
      "8 concurrent runs, dedicated queue",
      "Unlimited collaborators",
      "SAML SSO and audit logs",
      "Quarterly business review",
      "Priority support with same-day SLA",
    ],
  },
];

const faqs = [
  {
    q: "What does \"only pay for software that runs\" mean?",
    a: "Credits are deducted when Deft completes a working step — a passing build, a passing test suite, or a successful deploy. Planning, writing, and the verify loop are intentionally cheap, so you don't feel the meter while iterating. If a step fails and Deft can't recover, the credits for that step are not deducted from your balance.",
  },
  {
    q: "What is a deploy?",
    a: "A deploy is a publish to a live URL. Each Starter deploy gives you a unique preview.deft.computer subdomain. On Pro and Max you can attach a custom domain to any project.",
  },
  {
    q: "Can I keep iterating on a deployed app?",
    a: "Yes. Every redeploy of the same project counts as one deploy. On Pro and Max, deploys are unlimited, so iteration on a live project doesn't change your budget.",
  },
  {
    q: "Can I upgrade or downgrade?",
    a: "Yes. Changes take effect at your next billing cycle. The billing portal lives in your Account page.",
  },
  {
    q: "What happens when a run hits the ceiling?",
    a: "You set a credit ceiling before each run. Deft stops at that ceiling and hands you what it has — a partial receipt, never a surprise charge. To continue, raise the ceiling or top up.",
  },
  {
    q: "Can I bring my own API keys?",
    a: "Yes. The Vault page lets you store keys (OpenAI, Anthropic, Stripe, Supabase, and so on) encrypted on-device. Reference them in any prompt with $KEY_NAME and Deft injects them at runtime; keys never touch the database in plaintext.",
  },
];

const topups = [
  { id: "topup_small", name: "Small pack", price: 10, credits: 1_000 },
  { id: "topup_medium", name: "Medium pack", price: 30, credits: 3_000 },
  { id: "topup_large", name: "Large pack", price: 150, credits: 15_000 },
];

export default function Pricing() {
  usePageHead({
    title: "Pricing",
    description: "Honest credit-based pricing. Pay $0.01 per credit. Free 500 credits to start. Plans from $29 to $699 a month.",
    path: "/pricing",
  });

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
          <div className="mx-auto mb-5 inline-flex items-center gap-2 rounded-full border border-border/70 bg-surface-1/70 px-3 py-1 text-[12px] font-medium tracking-[0.01em] text-muted-foreground backdrop-blur">
            <span className="size-1.5 rounded-full bg-deploy animate-pulse" />
            Pricing
          </div>
          <h1 className="mx-auto max-w-3xl text-balance text-[40px] font-semibold leading-[1.05] tracking-[-0.025em] md:text-[64px]">
            You only pay for
            <br />
            <span className="text-deploy">software that runs.</span>
          </h1>
          <p className="mx-auto mt-6 max-w-xl text-[15.5px] leading-[1.65] text-muted-foreground">
            Planning, writing, and verifying are free. Credits charge only against successful work.
            If a step fails and {BRAND.name} can{"\u2019"}t recover, the credits for that step are
            not deducted from your balance.
          </p>
        </div>
      </section>

      {/* Plans */}
      <section className="relative pb-24">
        <div className="container-deft">
          <div className="mx-auto grid max-w-7xl gap-5 sm:grid-cols-2 lg:grid-cols-4">
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
                  <div className="absolute -top-3 left-7 inline-flex items-center gap-1.5 rounded-full border border-accent/60 bg-background px-2.5 py-0.5 text-[11.5px] font-medium tracking-[0.01em] text-foreground">
                    <Sparkles size={10} className="text-accent" aria-hidden />
                    Most popular
                  </div>
                )}

                <div>
                  <p className="text-[12px] font-semibold tracking-[0.01em] text-foreground">
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
                <p className="mt-1 text-[11px] text-muted-foreground/80">
                  ≈ ${plan.credits / 100} of compute at 1c = $0.01
                </p>

                {/* Three-up plan stats */}
                <div className="mt-6 grid grid-cols-3 gap-2 rounded-lg border border-border/60 bg-background/50 p-3 text-center">
                  <Stat icon={<Sparkles size={11} className="text-deploy" />} label={plan.deploysShort} caption="deploys" />
                  <Stat icon={<Globe size={11} className="text-accent" />} label={plan.domainShort} caption="domains" />
                  <Stat icon={<ShieldCheck size={11} className="text-foreground/80" />} label={plan.seatsShort} caption="seats" />
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

          <p className="mt-10 text-center text-[12.5px] leading-[1.7] text-muted-foreground">
            Every paid plan is annotated honestly: 1 credit = $0.01 of compute.
            A run that needs $4 of compute deducts 400 credits, only after the
            step succeeds. Working at company scale?{" "}
            <Link to="/contact" className="text-foreground underline underline-offset-4 hover:no-underline">
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
            <p className="text-[12px] font-medium tracking-[0.02em] text-accent-strong">Top-ups</p>
            <h2 className="text-[28px] font-semibold leading-[1.1] tracking-[-0.02em] md:text-[36px]">
              Need more credits this month?
            </h2>
            <p className="max-w-xl text-[14.5px] leading-[1.65] text-muted-foreground">
              Top-ups apply instantly and don{"\u2019"}t expire while your subscription is active.
            </p>
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            {topups.map((tu) => (
              <div
                key={tu.id}
                className="flex flex-col rounded-xl border border-border/70 bg-surface-1 p-6 transition-colors hover:border-border"
              >
                <p className="text-[12px] font-semibold tracking-[0.01em] text-foreground">
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
                  {loadingPlanId === tu.id ? <Loader2 size={14} className="animate-spin" /> : <>Add credits <ArrowRight size={13} /></>}
                </button>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* FAQ */}
      <section className="relative py-24">
        <div className="container-deft mx-auto max-w-3xl">
          <p className="text-[12px] font-medium tracking-[0.02em] text-accent-strong">FAQ</p>
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
      <span className="text-[11px] tracking-[0.01em] text-muted-foreground">{caption}</span>
    </div>
  );
}
