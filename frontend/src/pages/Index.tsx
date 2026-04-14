import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";

export default function Index() {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      {/* Hero — full viewport, massive type, Apple-level whitespace */}
      <section className="relative flex min-h-[100vh] items-center">
        <div className="mx-auto w-full max-w-7xl px-6">
          <div className="max-w-4xl pt-16">
            <ScrollReveal>
              <h1 className="font-serif text-3xl font-semibold leading-[1.08] tracking-[-0.02em] text-foreground sm:text-5xl md:text-7xl lg:text-[5.2rem]">
                The AI that
                <br />
                investigates.
              </h1>
            </ScrollReveal>
            <ScrollReveal delay={150}>
              <p className="mt-8 max-w-xl text-lg leading-[1.7] text-muted-foreground md:text-xl">
                Mariana has its own computer. It reads filings, writes programs,
                calls live APIs, builds financial models, and delivers finished
                research — autonomously.
              </p>
            </ScrollReveal>
            <ScrollReveal delay={300}>
              <div className="mt-10 flex flex-wrap items-center gap-5">
                <Link
                  to="/chat"
                  className="inline-flex items-center gap-2.5 rounded-md bg-primary px-6 py-3 text-sm font-medium text-primary-foreground transition-all hover:bg-primary/90 hover:shadow-xl hover:shadow-primary/10"
                >
                  Try Mariana <ArrowRight size={15} />
                </Link>
                <Link
                  to="/mariana"
                  className="text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
                >
                  How it works →
                </Link>
              </div>
            </ScrollReveal>
          </div>
        </div>

        {/* Subtle scroll indicator */}
        <div className="absolute bottom-10 left-1/2 -translate-x-1/2">
          <div className="h-10 w-px bg-gradient-to-b from-transparent to-border" />
        </div>
      </section>

      {/* What makes it different — editorial long-form */}
      <section className="bg-secondary/30">
        <div className="mx-auto max-w-7xl px-6 py-16 md:py-32">
          <div className="grid gap-20 lg:grid-cols-[1fr_400px]">
            <div>
              <ScrollReveal>
                <h2 className="font-serif text-3xl font-semibold leading-[1.15] text-foreground md:text-4xl lg:text-[2.75rem]">
                  Most research stops
                  <br className="hidden md:block" />
                  at the surface.
                </h2>
              </ScrollReveal>
              <ScrollReveal delay={100}>
                <div className="mt-8 space-y-6 text-[16px] leading-[1.8] text-muted-foreground">
                  <p>
                    A standard AI tool summarizes what's already been written.
                    Mariana goes further. It pulls raw SEC filings and finds
                    references to unnamed contract manufacturers. It writes a
                    custom scraper to cross-reference trade registry data across
                    jurisdictions. It identifies pricing anomalies that suggest an
                    undisclosed related-party arrangement.
                  </p>
                  <p>
                    Then it builds a regression model to quantify the impact, runs
                    a Monte Carlo simulation across six macro scenarios, and
                    packages the findings into a finished deliverable — a PDF, a
                    slide deck, an interactive dashboard, or a custom web
                    application. Whatever format your team needs.
                  </p>
                  <p className="text-foreground">
                    The question isn't how long it takes. It's whether you're
                    seeing everything there is to see.
                  </p>
                </div>
              </ScrollReveal>
            </div>

            <ScrollReveal delay={250} className="self-start lg:mt-14">
              <div className="rounded-lg bg-card p-6 shadow-sm ring-1 ring-border">
                <p className="mb-5 text-[11px] font-medium uppercase tracking-[0.15em] text-muted-foreground">
                  Illustrative — what Mariana does on a single query
                </p>
                <div className="space-y-1.5 overflow-x-auto font-mono text-xs leading-6 text-muted-foreground">
                  <p className="text-foreground">$ mariana status</p>
                  <p>▸ filings parsed: 847</p>
                  <p>▸ programs written: 14</p>
                  <p>▸ api calls: 2,300+</p>
                  <p>▸ lines of code generated: ~3,000</p>
                  <p>▸ data sources queried: 43</p>
                  <p>▸ models built: 6</p>
                  <div className="mt-4 border-t border-border pt-4">
                    <p className="text-foreground">▸ output: PDF report + interactive model</p>
                    <p className="text-accent">▸ status: complete</p>
                  </div>
                </div>
              </div>
            </ScrollReveal>
          </div>
        </div>
      </section>

      {/* Two pillars */}
      <section className="mx-auto max-w-7xl px-6 py-16 md:py-32">
        <div className="grid gap-12 md:gap-16 md:grid-cols-2">
          <ScrollReveal>
            <div className="border-l-2 border-accent/40 pl-8">
              <h2 className="font-serif text-2xl font-semibold text-foreground md:text-3xl">
                Published Research
              </h2>
              <p className="mt-4 text-[15px] leading-[1.8] text-muted-foreground">
                Read our work first. Institutional-grade analysis across
                equities, macro, fixed income, commodities, and crypto — some
                available free, premium reports for token holders.
              </p>
              <Link
                to="/research"
                className="mt-5 inline-flex items-center gap-2 text-sm font-medium text-foreground transition-colors hover:text-accent"
              >
                Browse research <ArrowRight size={14} />
              </Link>
            </div>
          </ScrollReveal>

          <ScrollReveal delay={150}>
            <div className="border-l-2 border-accent/40 pl-8">
              <h2 className="font-serif text-2xl font-semibold text-foreground md:text-3xl">
                Mariana Computer
              </h2>
              <p className="mt-4 text-[15px] leading-[1.8] text-muted-foreground">
                Run your own investigation. Mariana has full computer access —
                it writes and executes code, builds applications, calls external
                APIs, and works until the research is done. No human in the loop.
              </p>
              <Link
                to="/mariana"
                className="mt-5 inline-flex items-center gap-2 text-sm font-medium text-foreground transition-colors hover:text-accent"
              >
                Learn more <ArrowRight size={14} />
              </Link>
            </div>
          </ScrollReveal>
        </div>
      </section>

      {/* Endurance — key differentiator */}
      <section className="bg-secondary/30">
        <div className="mx-auto max-w-7xl px-6 py-16 md:py-32">
          <div className="max-w-3xl">
            <ScrollReveal>
              <h2 className="font-serif text-3xl font-semibold leading-[1.15] text-foreground md:text-4xl lg:text-[2.75rem]">
                Hours of depth,
                <br className="hidden md:block" />
                without degradation.
              </h2>
            </ScrollReveal>
            <ScrollReveal delay={100}>
              <div className="mt-8 space-y-6 text-[16px] leading-[1.8] text-muted-foreground">
                <p>
                  Most AI loses coherence after a few minutes. Mariana is
                  architected for endurance — it maintains full context and
                  analytical precision across research sessions that run for
                  hours, not minutes.
                </p>
                <p>
                  A Flagship investigation can run for 12+ hours continuously:
                  reading filings, writing code, testing hypotheses, refining
                  models — all without losing track of what it's doing or why.
                  The longer it runs, the deeper it gets.
                </p>
              </div>
            </ScrollReveal>
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="mx-auto max-w-7xl px-6 py-16 md:py-32">
        <ScrollReveal>
          <div className="max-w-xl">
            <h2 className="font-serif text-3xl font-semibold text-foreground md:text-4xl lg:text-[2.75rem]">
              Start with a single question.
            </h2>
            <p className="mt-4 text-[16px] leading-[1.7] text-muted-foreground">
              Every account gets $5 in free credits. No commitment, no
              subscription required. Pay as you go after that.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-5">
              <Link
                to="/signup"
                className="inline-flex items-center gap-2.5 rounded-md bg-primary px-6 py-3 text-sm font-medium text-primary-foreground transition-all hover:bg-primary/90 hover:shadow-xl hover:shadow-primary/10"
              >
                Create free account <ArrowRight size={15} />
              </Link>
              <Link
                to="/pricing"
                className="text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
              >
                View pricing →
              </Link>
            </div>
          </div>
        </ScrollReveal>
      </section>

      <Footer />
    </div>
  );
}
