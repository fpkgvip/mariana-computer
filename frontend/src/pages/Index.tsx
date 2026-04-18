import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Link } from "react-router-dom";
import { ArrowRight, Zap, Code, BarChart3, FileText, Globe, Cpu } from "lucide-react";

const capabilities = [
  {
    icon: FileText,
    title: "Reads filings",
    desc: "Parses SEC filings, earnings transcripts, and corporate registries autonomously.",
  },
  {
    icon: Code,
    title: "Writes programs",
    desc: "Generates custom scrapers, models, and analysis tools on the fly.",
  },
  {
    icon: Globe,
    title: "Queries APIs",
    desc: "Connects to financial data providers, trade registries, and live data feeds.",
  },
  {
    icon: BarChart3,
    title: "Builds models",
    desc: "Constructs regressions, Monte Carlo simulations, and factor models.",
  },
  {
    icon: Cpu,
    title: "Runs for hours",
    desc: "Maintains context across 12+ hour research sessions without degradation.",
  },
  {
    icon: Zap,
    title: "Delivers results",
    desc: "Produces finished PDFs, slide decks, dashboards, and web applications.",
  },
];

export default function Index() {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      {/* Hero */}
      <section className="relative flex min-h-[100vh] items-center overflow-hidden">
        {/* Subtle gradient background */}
        <div className="pointer-events-none absolute inset-0">
          <div className="absolute -top-40 right-0 h-[600px] w-[600px] rounded-full bg-primary/5 blur-3xl dark:bg-primary/10" />
          <div className="absolute bottom-0 left-0 h-[400px] w-[400px] rounded-full bg-primary/3 blur-3xl dark:bg-primary/5" />
        </div>

        <div className="relative mx-auto w-full max-w-7xl px-6">
          <div className="max-w-4xl pt-16">
            <ScrollReveal>
              <div className="mb-6 inline-flex items-center gap-2 rounded-full border border-border bg-card/80 px-4 py-1.5 text-sm text-muted-foreground backdrop-blur-sm">
                <div className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse-glow" />
                Autonomous financial research
              </div>
            </ScrollReveal>
            <ScrollReveal delay={80}>
              <h1 className="text-4xl font-bold leading-[1.08] tracking-tight text-foreground sm:text-5xl md:text-6xl lg:text-7xl">
                The AI that
                <br />
                <span className="text-gradient">investigates.</span>
              </h1>
            </ScrollReveal>
            <ScrollReveal delay={160}>
              <p className="mt-6 max-w-xl text-lg leading-relaxed text-muted-foreground">
                Mariana has its own computer. It reads filings, writes programs,
                calls live APIs, builds financial models, and delivers finished
                research — autonomously.
              </p>
            </ScrollReveal>
            <ScrollReveal delay={240}>
              <div className="mt-8 flex flex-wrap items-center gap-4">
                <Link
                  to="/chat"
                  className="inline-flex items-center gap-2 rounded-lg bg-primary px-6 py-3 text-sm font-semibold text-primary-foreground shadow-md transition-all hover:opacity-90 hover:shadow-lg"
                >
                  Try Mariana <ArrowRight size={15} />
                </Link>
                <Link
                  to="/mariana"
                  className="inline-flex items-center gap-2 rounded-lg border border-border bg-card/50 px-6 py-3 text-sm font-semibold text-foreground backdrop-blur-sm transition-all hover:bg-card hover:shadow-sm"
                >
                  How it works
                </Link>
              </div>
            </ScrollReveal>
          </div>
        </div>

        {/* Scroll indicator */}
        <div className="absolute bottom-8 left-1/2 -translate-x-1/2">
          <div className="h-8 w-px bg-gradient-to-b from-transparent to-border" />
        </div>
      </section>

      {/* What makes it different */}
      <section className="border-y border-border bg-card/30">
        <div className="mx-auto max-w-7xl px-6 py-20 md:py-32">
          <div className="grid gap-16 lg:grid-cols-[1fr_380px]">
            <div>
              <ScrollReveal>
                <h2 className="text-3xl font-bold leading-tight tracking-tight text-foreground md:text-4xl">
                  Most research stops
                  <br className="hidden md:block" />
                  at the surface.
                </h2>
              </ScrollReveal>
              <ScrollReveal delay={100}>
                <div className="mt-8 space-y-5 text-base leading-relaxed text-muted-foreground">
                  <p>
                    A standard AI tool summarizes what's already been written.
                    Mariana goes further. It pulls raw SEC filings and finds
                    references to unnamed contract manufacturers. It writes a
                    custom program to cross-reference trade registry data across
                    jurisdictions. It identifies pricing anomalies that suggest an
                    undisclosed related-party arrangement.
                  </p>
                  <p>
                    Then it builds a regression model to quantify the impact, runs
                    a Monte Carlo simulation across six macro scenarios, and
                    packages the findings into a finished deliverable — a PDF, a
                    slide deck, an interactive dashboard, or a custom web
                    application.
                  </p>
                  <p className="font-medium text-foreground">
                    The question isn't how long it takes. It's whether you're
                    seeing everything there is to see.
                  </p>
                </div>
              </ScrollReveal>
            </div>

            <ScrollReveal delay={200} className="self-start lg:mt-8">
              <div className="rounded-xl border border-border bg-card p-6 shadow-sm">
                <p className="mb-4 text-xs font-semibold uppercase tracking-widest text-muted-foreground">
                  Single query output
                </p>
                <div className="space-y-1 overflow-x-auto font-mono text-xs leading-7 text-muted-foreground">
                  <p className="text-foreground">$ mariana status</p>
                  <p>▸ filings parsed: 847</p>
                  <p>▸ programs written: 14</p>
                  <p>▸ api calls: 2,300+</p>
                  <p>▸ lines of code generated: ~3,000</p>
                  <p>▸ data sources queried: 43</p>
                  <p>▸ models built: 6</p>
                  <div className="mt-3 border-t border-border pt-3">
                    <p className="text-foreground">▸ output: PDF report + interactive model</p>
                    <p className="text-primary">▸ status: complete</p>
                  </div>
                </div>
              </div>
            </ScrollReveal>
          </div>
        </div>
      </section>

      {/* Capabilities grid */}
      <section className="mx-auto max-w-7xl px-6 py-20 md:py-32">
        <ScrollReveal>
          <h2 className="text-3xl font-bold tracking-tight text-foreground md:text-4xl">
            What Mariana does
          </h2>
          <p className="mt-3 max-w-lg text-base text-muted-foreground">
            A full research workstation — not a chatbot.
          </p>
        </ScrollReveal>
        <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {capabilities.map((cap, i) => (
            <ScrollReveal key={cap.title} delay={i * 60}>
              <div className="group rounded-xl border border-border bg-card p-6 transition-all hover:border-primary/20 hover:shadow-sm">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <cap.icon size={20} />
                </div>
                <h3 className="mt-4 text-sm font-bold text-foreground">{cap.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{cap.desc}</p>
              </div>
            </ScrollReveal>
          ))}
        </div>
      </section>

      {/* Endurance */}
      <section className="border-y border-border bg-card/30">
        <div className="mx-auto max-w-7xl px-6 py-20 md:py-32">
          <div className="max-w-3xl">
            <ScrollReveal>
              <h2 className="text-3xl font-bold leading-tight tracking-tight text-foreground md:text-4xl">
                Hours of depth,
                <br className="hidden md:block" />
                without degradation.
              </h2>
            </ScrollReveal>
            <ScrollReveal delay={100}>
              <div className="mt-8 space-y-5 text-base leading-relaxed text-muted-foreground">
                <p>
                  Most AI loses coherence after a few minutes. Mariana is
                  architected for endurance — it maintains full context and
                  analytical precision across research sessions that run for
                  hours, not minutes.
                </p>
                <p>
                  A deep investigation can run for 12+ hours continuously:
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
      <section className="mx-auto max-w-7xl px-6 py-20 md:py-32">
        <ScrollReveal>
          <div className="max-w-xl">
            <h2 className="text-3xl font-bold tracking-tight text-foreground md:text-4xl">
              Start with a single question.
            </h2>
            <p className="mt-4 text-base leading-relaxed text-muted-foreground">
              Every account includes research credits to get started.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-4">
              <Link
                to="/signup"
                className="inline-flex items-center gap-2 rounded-lg bg-primary px-6 py-3 text-sm font-semibold text-primary-foreground shadow-md transition-all hover:opacity-90 hover:shadow-lg"
              >
                Get started <ArrowRight size={15} />
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
