import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Link } from "react-router-dom";
import { ArrowRight, Check } from "lucide-react";

const faqs = [
  {
    q: "What are tokens?",
    a: "Tokens are the unit of usage for Mariana. Each query consumes tokens based on the depth of research, model selected, data sources accessed, and compute used. $1 buys 10 tokens.",
  },
  {
    q: "How do I know how many tokens a query will use?",
    a: "Before Mariana begins, it estimates the token cost based on the scope of the query and your selected research depth and model tier. You approve the estimate before any tokens are consumed. Actual costs may vary — AI workloads are inherently unpredictable.",
  },
  {
    q: "What model tiers are available?",
    a: "Four tiers: Cheap (fastest, least capable), Fast (good balance), Pro (high-quality reasoning), and Frontier (Claude Opus — maximum depth and capability). Higher tiers consume more tokens per query.",
  },
  {
    q: "Do unused tokens expire?",
    a: "Tokens are valid for 12 months from purchase. Enterprise plans can negotiate custom terms.",
  },
  {
    q: "What data sources does Mariana access?",
    a: "SEC EDGAR, earnings transcripts, corporate registries, academic databases, and select financial data providers. Enterprise clients can connect their own proprietary data feeds and terminal credentials.",
  },
  {
    q: "What does Enterprise include?",
    a: "A flat $5,000/month fee covers enterprise features: SLA guarantees, a dedicated research liaison, custom integrations, and priority compute. Token usage is billed separately at volume rates. No sales call required — sign up directly.",
  },
  {
    q: "What is the Custom plan?",
    a: "Starting at $10,000/month, Custom plans include everything in Enterprise plus dedicated infrastructure, locally hosted models, custom dashboards, and bespoke research workflows. Contact us to scope your deployment.",
  },
];

export default function Pricing() {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <div className="mx-auto max-w-7xl px-6 pb-24 pt-32 md:pt-40">
        <ScrollReveal>
          <h1 className="font-serif text-3xl font-semibold leading-[1.08] tracking-[-0.02em] text-foreground sm:text-4xl md:text-5xl lg:text-[3.5rem]">
            Simple, transparent pricing.
          </h1>
          <p className="mt-6 max-w-lg text-lg leading-[1.7] text-muted-foreground">
            Every account gets full access to Mariana. Pay only for what you use.
            No subscriptions, no per-seat fees.
          </p>
        </ScrollReveal>

        {/* Three-column: Pay-as-you-go + Enterprise + Custom */}
        <div className="mt-12 grid gap-6 sm:mt-16 md:grid-cols-2 lg:grid-cols-3">
          {/* Pay as you go */}
          <ScrollReveal>
            <div className="flex h-full flex-col rounded-lg bg-card p-8 shadow-sm ring-1 ring-border">
              <p className="text-xs font-medium uppercase tracking-[0.15em] text-muted-foreground">
                Pay as you go
              </p>
              <h2 className="mt-4 font-serif text-4xl font-semibold text-foreground">
                $0<span className="text-lg font-normal text-muted-foreground">/month</span>
              </h2>
              <p className="mt-2 text-sm text-muted-foreground">
                $1 = 10 tokens. $5 free credit on signup.
              </p>

              <div className="my-6 border-t border-border" />

              <ul className="flex-1 space-y-3">
                {[
                  "Full access to Mariana Computer",
                  "All research depth tiers",
                  "All model tiers (Cheap → Frontier)",
                  "PDF, dashboard, and app deliverables",
                  "Published research library access",
                  "$5 free credit on every new account",
                ].map((f) => (
                  <li key={f} className="flex items-start gap-2.5 text-[15px] text-foreground">
                    <Check size={15} className="mt-0.5 shrink-0 text-accent" strokeWidth={2} />
                    {f}
                  </li>
                ))}
              </ul>

              <div className="mt-8">
                <Link
                  to="/signup"
                  className="flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-3 text-sm font-medium text-primary-foreground transition-all hover:bg-primary/90"
                >
                  Get started free <ArrowRight size={15} />
                </Link>
              </div>
            </div>
          </ScrollReveal>

          {/* Enterprise */}
          <ScrollReveal delay={150}>
            <div className="flex h-full flex-col rounded-lg bg-card p-8 shadow-sm ring-1 ring-border">
              <p className="text-xs font-medium uppercase tracking-[0.15em] text-muted-foreground">
                Enterprise
              </p>
              <h2 className="mt-4 font-serif text-4xl font-semibold text-foreground">
                $5,000<span className="text-lg font-normal text-muted-foreground">/month</span>
              </h2>
              <p className="mt-2 text-sm text-muted-foreground">
                Plus token usage at volume rates.
              </p>

              <div className="my-6 border-t border-border" />

              <ul className="flex-1 space-y-3">
                {[
                  "Everything in pay-as-you-go",
                  "SLA guarantees",
                  "Dedicated research liaison",
                  "Custom data source integration",
                  "Priority compute allocation",
                  "Volume token pricing",
                ].map((f) => (
                  <li key={f} className="flex items-start gap-2.5 text-[15px] text-foreground">
                    <Check size={15} className="mt-0.5 shrink-0 text-accent" strokeWidth={2} />
                    {f}
                  </li>
                ))}
              </ul>

              <div className="mt-8">
                <Link
                  to="/signup"
                  className="flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-3 text-sm font-medium text-primary-foreground transition-all hover:bg-primary/90"
                >
                  Get started <ArrowRight size={15} />
                </Link>
              </div>
            </div>
          </ScrollReveal>

          {/* Custom */}
          <ScrollReveal delay={300}>
            <div className="flex h-full flex-col rounded-lg bg-card p-8 shadow-sm ring-1 ring-border">
              <p className="text-xs font-medium uppercase tracking-[0.15em] text-muted-foreground">
                Custom
              </p>
              <h2 className="mt-4 font-serif text-4xl font-semibold text-foreground">
                $10,000+<span className="text-lg font-normal text-muted-foreground">/month</span>
              </h2>
              <p className="mt-2 text-sm text-muted-foreground">
                Dedicated infrastructure. Bespoke deployment.
              </p>

              <div className="my-6 border-t border-border" />

              <ul className="flex-1 space-y-3">
                {[
                  "Everything in Enterprise",
                  "Dedicated infrastructure",
                  "Locally hosted models",
                  "Custom research dashboards",
                  "Bespoke workflows and pipelines",
                  "On-premise deployment",
                  "White-glove onboarding",
                ].map((f) => (
                  <li key={f} className="flex items-start gap-2.5 text-[15px] text-foreground">
                    <Check size={15} className="mt-0.5 shrink-0 text-accent" strokeWidth={2} />
                    {f}
                  </li>
                ))}
              </ul>

              <div className="mt-8">
                <Link to="/contact" className="flex w-full items-center justify-center gap-2 rounded-md border border-border px-4 py-3 text-sm font-medium text-foreground transition-all hover:bg-secondary">
                  Contact sales
                </Link>
              </div>
            </div>
          </ScrollReveal>
        </div>

        {/* How tokens work */}
        <div className="mt-24">
          <ScrollReveal>
            <h2 className="font-serif text-2xl font-semibold text-foreground md:text-3xl">
              How tokens work
            </h2>
          </ScrollReveal>
          <ScrollReveal delay={100}>
            <div className="mt-8 grid gap-6 sm:grid-cols-2 md:grid-cols-3">
              {[
                {
                  step: "01",
                  title: "Submit a query",
                  desc: "Choose your research depth and model tier. Mariana estimates the token cost before it begins.",
                },
                {
                  step: "02",
                  title: "Approve and run",
                  desc: "Review the estimate and approve. Mariana begins autonomous research — you can close your browser.",
                },
                {
                  step: "03",
                  title: "Receive deliverables",
                  desc: "Get notified when research is complete. Download reports, models, dashboards, or whatever Mariana built.",
                },
              ].map((s) => (
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
