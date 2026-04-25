import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { BRAND } from "@/lib/brand";

/**
 * Deft v1 landing — calm autonomous-operator positioning.
 * Promise a ceiling, deliver a receipt. Tight typography, dark-first,
 * no editorial flourish that contradicts the operator voice.
 */
export default function Index() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <Navbar />

      {/* Hero */}
      <section className="relative flex min-h-[92vh] items-center">
        <div className="container-deft pt-16">
          <div className="max-w-3xl">
            <ScrollReveal>
              <div className="mb-8 inline-flex items-center gap-2 rounded-full border border-border bg-surface-1 px-3 py-1 text-[11px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
                <span className="size-1.5 rounded-full bg-success animate-pulse" />
                Long-running coding agent
              </div>
            </ScrollReveal>
            <ScrollReveal delay={100}>
              <h1 className="text-4xl font-semibold leading-[1.04] tracking-[-0.03em] sm:text-6xl md:text-7xl lg:text-[5.25rem]">
                Set a ceiling.
                <br />
                Get a receipt.
              </h1>
            </ScrollReveal>
            <ScrollReveal delay={200}>
              <p className="mt-7 max-w-xl text-lg leading-[1.65] text-ink-1 md:text-xl">
                {BRAND.name} is an autonomous coding agent for vibe coders and
                technical prosumers. Hand it a goal and a credit ceiling.
                It plans, executes, tests, and delivers a finished result —
                no babysitting, no surprise bills.
              </p>
            </ScrollReveal>
            <ScrollReveal delay={300}>
              <div className="mt-10 flex flex-wrap items-center gap-4">
                <Link
                  to="/signup"
                  className="inline-flex items-center gap-2 rounded-md bg-primary px-5 py-3 text-sm font-medium text-primary-foreground shadow-elev-2 transition-all duration-fast ease-out-expo hover:bg-primary/90 hover:shadow-elev-3"
                >
                  Start a task <ArrowRight size={15} />
                </Link>
                <Link
                  to="/product"
                  className="inline-flex items-center gap-2 rounded-md border border-border bg-surface-1 px-5 py-3 text-sm font-medium text-foreground transition-colors hover:bg-surface-2"
                >
                  How it works
                </Link>
                <Link
                  to="/pricing"
                  className="text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
                >
                  Pricing →
                </Link>
              </div>
            </ScrollReveal>
          </div>
        </div>

        {/* Subtle scroll indicator */}
        <div className="absolute bottom-8 left-1/2 -translate-x-1/2 opacity-60">
          <div className="h-10 w-px bg-gradient-to-b from-transparent to-border" />
        </div>
      </section>

      {/* What it does */}
      <section className="border-t border-border bg-surface-1">
        <div className="container-deft py-20 md:py-32">
          <div className="grid gap-16 lg:grid-cols-[1fr_440px]">
            <div>
              <ScrollReveal>
                <h2 className="text-3xl font-semibold leading-[1.1] tracking-[-0.02em] md:text-5xl">
                  Most AI tools stop
                  <br className="hidden md:block" />
                  at the chat window.
                </h2>
              </ScrollReveal>
              <ScrollReveal delay={100}>
                <div className="mt-8 space-y-6 text-[16px] leading-[1.8] text-ink-1">
                  <p>
                    A standard assistant summarizes what's already been
                    written. {BRAND.name} goes further. It reads primary
                    sources, writes and runs its own code, queries live
                    APIs, browses the web, and assembles the finished
                    deliverable — a working app, a clean dataset, a
                    polished report, a tested integration.
                  </p>
                  <p>
                    Every run is bounded by a credit ceiling you set
                    up-front. {BRAND.name} stops when it's done or when
                    it hits the cap, then hands you a full receipt:
                    plan, steps taken, files produced, credits spent.
                  </p>
                  <p className="text-foreground">
                    The question isn't what an AI can tell you. It's
                    what it can finish for you — and what that finish
                    cost.
                  </p>
                </div>
              </ScrollReveal>
            </div>

            <ScrollReveal delay={200} className="self-start lg:mt-12">
              <div className="rounded-xl border border-border bg-surface-2 p-6 shadow-elev-1">
                <p className="mb-5 font-mono text-[10.5px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
                  $ deft status
                </p>
                <div className="space-y-1.5 overflow-x-auto font-mono text-xs leading-6 text-ink-1 tabular">
                  <p><span className="text-muted-foreground">▸</span> goal: ship competitor teardown</p>
                  <p><span className="text-muted-foreground">▸</span> ceiling: 800 credits ($8.00)</p>
                  <p><span className="text-muted-foreground">▸</span> sources read: 142</p>
                  <p><span className="text-muted-foreground">▸</span> programs written: 11</p>
                  <p><span className="text-muted-foreground">▸</span> api calls: 1,830</p>
                  <p><span className="text-muted-foreground">▸</span> tests passed: 24/24</p>
                  <div className="mt-4 border-t border-border pt-4">
                    <p>▸ output: PDF · XLSX · 12-slide deck</p>
                    <p>▸ spent: <span className="text-foreground">617 credits ($6.17)</span></p>
                    <p>▸ status: <span className="text-success">delivered</span></p>
                  </div>
                </div>
              </div>
            </ScrollReveal>
          </div>
        </div>
      </section>

      {/* Three pillars */}
      <section className="container-deft py-20 md:py-32">
        <ScrollReveal>
          <h2 className="mb-14 max-w-3xl text-3xl font-semibold leading-[1.12] tracking-[-0.02em] md:text-5xl">
            One agent. Every part of the workflow.
          </h2>
        </ScrollReveal>
        <div className="grid gap-10 md:gap-14 md:grid-cols-3">
          {[
            {
              h: "Code & ship",
              p: "Full-stack apps, scripts, and tested CI pipelines. {n} writes, runs, and debugs its own work until the build is green and the deploy succeeds.",
            },
            {
              h: "Research & analyze",
              p: "Multi-source research with inline citations, market sizing, competitive deep dives. Delivered as a publishable report or structured dataset.",
            },
            {
              h: "Operate & automate",
              p: "Scheduled briefings, browser automation across logged-in tools, mailbox triage, data pipeline checks — on a cron you define.",
            },
          ].map((card, i) => (
            <ScrollReveal key={card.h} delay={i * 80}>
              <div className="border-l-2 border-accent/60 pl-7">
                <h3 className="text-xl font-semibold tracking-tight md:text-2xl">{card.h}</h3>
                <p className="mt-4 text-[15px] leading-[1.8] text-ink-1">
                  {card.p.replace("{n}", BRAND.name)}
                </p>
              </div>
            </ScrollReveal>
          ))}
        </div>
      </section>

      {/* Example prompts */}
      <section className="border-t border-border bg-surface-1">
        <div className="container-deft py-20 md:py-32">
          <ScrollReveal>
            <h2 className="max-w-3xl text-3xl font-semibold leading-[1.12] tracking-[-0.02em] md:text-5xl">
              Real prompts, real receipts.
            </h2>
          </ScrollReveal>
          <ScrollReveal delay={100}>
            <div className="mt-12 grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {[
                "Build a 30-page competitor teardown of our top 5 rivals with a pricing matrix and a SWOT.",
                "Take this 80-MB CSV, clean it, find anomalies, and produce an XLSX with charts and a short memo.",
                "Write a FastAPI service that ingests Stripe webhooks and writes to Supabase — with tests.",
                "Audit our marketing site for accessibility issues and fix the top ten with a PR.",
                "Review this 60-page MSA and flag anything non-standard against our template.",
                "Every Monday, scan 20 competitor career pages and summarize the hiring signal.",
              ].map((q) => (
                <div
                  key={q}
                  className="rounded-lg border border-border bg-surface-2 p-6 text-[14px] leading-[1.7] text-ink-1 transition-colors hover:bg-surface-3"
                >
                  <p>{q}</p>
                </div>
              ))}
            </div>
          </ScrollReveal>
        </div>
      </section>

      {/* Endurance */}
      <section className="container-deft py-20 md:py-32">
        <div className="max-w-3xl">
          <ScrollReveal>
            <h2 className="text-3xl font-semibold leading-[1.1] tracking-[-0.02em] md:text-5xl">
              Hours of depth,
              <br className="hidden md:block" />
              without degradation.
            </h2>
          </ScrollReveal>
          <ScrollReveal delay={100}>
            <div className="mt-8 space-y-6 text-[16px] leading-[1.8] text-ink-1">
              <p>
                Most agents lose coherence after a few minutes. {BRAND.name} is
                architected for endurance — full context across sessions that
                run for hours, not minutes.
              </p>
              <p>
                A deep job can run for 12+ hours continuously: reading,
                writing code, running tests, iterating on the deliverable.
                It checkpoints every step, recovers from interruption, and
                refuses to spend past the ceiling you set.
              </p>
            </div>
          </ScrollReveal>
        </div>
      </section>

      {/* CTA */}
      <section className="border-t border-border bg-surface-1">
        <div className="container-deft py-20 md:py-32">
          <ScrollReveal>
            <div className="max-w-xl">
              <h2 className="text-3xl font-semibold tracking-[-0.02em] md:text-5xl">
                Start with a single task.
              </h2>
              <p className="mt-5 text-[16px] leading-[1.7] text-ink-1">
                Every account includes credits to put {BRAND.name} to work
                today — no setup, no integrations required.
              </p>
              <div className="mt-8 flex flex-wrap items-center gap-4">
                <Link
                  to="/signup"
                  className="inline-flex items-center gap-2 rounded-md bg-primary px-5 py-3 text-sm font-medium text-primary-foreground shadow-elev-2 transition-all duration-fast ease-out-expo hover:bg-primary/90 hover:shadow-elev-3"
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
        </div>
      </section>

      <Footer />
    </div>
  );
}
