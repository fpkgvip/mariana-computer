import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";

/**
 * v3.7 reposition: Mariana is now a general-purpose autonomous AI teammate
 * for B2B and power users, not a finance-only tool.  The hero, copy, and
 * example queries reflect the full breadth of what the agent actually does
 * in production (research, docs/decks, data, code, browser automation,
 * media, integrations) while preserving the editorial, calm tone.
 */
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
                actually does the work.
              </h1>
            </ScrollReveal>
            <ScrollReveal delay={150}>
              <p className="mt-8 max-w-xl text-lg leading-[1.7] text-muted-foreground md:text-xl">
                Mariana Computer is an autonomous teammate with its own
                computer. It researches, writes code, builds documents,
                designs decks, analyzes data, browses the web, and ships
                finished work — end to end.
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

      {/* What it does — editorial long-form */}
      <section className="bg-secondary/30">
        <div className="mx-auto max-w-7xl px-6 py-16 md:py-32">
          <div className="grid gap-20 lg:grid-cols-[1fr_400px]">
            <div>
              <ScrollReveal>
                <h2 className="font-serif text-3xl font-semibold leading-[1.15] text-foreground md:text-4xl lg:text-[2.75rem]">
                  Most AI tools stop
                  <br className="hidden md:block" />
                  at the chat window.
                </h2>
              </ScrollReveal>
              <ScrollReveal delay={100}>
                <div className="mt-8 space-y-6 text-[16px] leading-[1.8] text-muted-foreground">
                  <p>
                    A standard assistant summarizes what's already been
                    written. Mariana goes further. It plans the job, reads
                    the primary sources, writes and runs its own code,
                    queries live APIs, browses the web, generates images,
                    and assembles the finished deliverable — a 40-page
                    report, a financial model with live formulas, a
                    polished slide deck, a working web app, a clean
                    dataset, a ready-to-ship contract redline.
                  </p>
                  <p>
                    It works across every format your team already uses:
                    Markdown, Word, PowerPoint, Excel, PDF, CSV, Python,
                    TypeScript, SQL. It integrates with the tools you
                    already trust — Google Drive, Calendar, GitHub,
                    Supabase, Vercel, and more — through signed connectors
                    you control.
                  </p>
                  <p className="text-foreground">
                    The question isn't what an AI can tell you. It's what
                    it can finish for you.
                  </p>
                </div>
              </ScrollReveal>
            </div>

            <ScrollReveal delay={250} className="self-start lg:mt-14">
              <div className="rounded-lg bg-card p-6 shadow-sm ring-1 ring-border">
                <p className="mb-5 text-[11px] font-medium uppercase tracking-[0.15em] text-muted-foreground">
                  Illustrative — what Mariana did on a single brief
                </p>
                <div className="space-y-1.5 overflow-x-auto font-mono text-xs leading-6 text-muted-foreground">
                  <p className="text-foreground">$ mariana status</p>
                  <p>▸ sources read: 142</p>
                  <p>▸ programs written: 11</p>
                  <p>▸ api calls: 1,830+</p>
                  <p>▸ lines of code generated: ~2,400</p>
                  <p>▸ data tables built: 7</p>
                  <p>▸ integrations queried: Drive, GCal, GitHub</p>
                  <div className="mt-4 border-t border-border pt-4">
                    <p className="text-foreground">▸ output: PDF report + XLSX model + 12-slide deck</p>
                    <p className="text-accent">▸ status: delivered</p>
                  </div>
                </div>
              </div>
            </ScrollReveal>
          </div>
        </div>
      </section>

      {/* Three pillars — what you can ask it to do */}
      <section className="mx-auto max-w-7xl px-6 py-16 md:py-32">
        <ScrollReveal>
          <h2 className="mb-12 max-w-3xl font-serif text-3xl font-semibold leading-[1.15] text-foreground md:text-4xl">
            One agent. Every part of the workflow.
          </h2>
        </ScrollReveal>
        <div className="grid gap-12 md:gap-16 md:grid-cols-3">
          <ScrollReveal>
            <div className="border-l-2 border-accent/40 pl-8">
              <h3 className="font-serif text-xl font-semibold text-foreground md:text-2xl">
                Research &amp; analyze
              </h3>
              <p className="mt-4 text-[15px] leading-[1.8] text-muted-foreground">
                Multi-source deep research with inline citations. Market
                sizing, competitive analysis, literature reviews,
                regulatory scans. Delivered as a publishable PDF, DOCX,
                or structured dataset.
              </p>
            </div>
          </ScrollReveal>

          <ScrollReveal delay={100}>
            <div className="border-l-2 border-accent/40 pl-8">
              <h3 className="font-serif text-xl font-semibold text-foreground md:text-2xl">
                Build &amp; ship
              </h3>
              <p className="mt-4 text-[15px] leading-[1.8] text-muted-foreground">
                Spreadsheets with real formulas, presentations with
                generated imagery, full web apps and dashboards, clean
                code repos, automation scripts. Mariana writes, runs, and
                debugs its own work until it's done.
              </p>
            </div>
          </ScrollReveal>

          <ScrollReveal delay={200}>
            <div className="border-l-2 border-accent/40 pl-8">
              <h3 className="font-serif text-xl font-semibold text-foreground md:text-2xl">
                Operate &amp; automate
              </h3>
              <p className="mt-4 text-[15px] leading-[1.8] text-muted-foreground">
                Scheduled tasks, recurring briefings, browser automation
                across logged-in tools, mailbox triage, calendar
                coordination, CRM enrichment, data pipeline checks. On a
                cron you define.
              </p>
            </div>
          </ScrollReveal>
        </div>
      </section>

      {/* Example prompts — concrete, broad */}
      <section className="bg-secondary/30">
        <div className="mx-auto max-w-7xl px-6 py-16 md:py-32">
          <ScrollReveal>
            <h2 className="max-w-3xl font-serif text-3xl font-semibold leading-[1.15] text-foreground md:text-4xl">
              Things teams actually ship with Mariana.
            </h2>
          </ScrollReveal>
          <ScrollReveal delay={100}>
            <div className="mt-12 grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {[
                "Build a 30-page competitive teardown of our top 5 rivals with a pricing matrix and a SWOT.",
                "Take this 80-MB CSV, clean it, find anomalies, and produce an XLSX with charts and a short memo.",
                "Draft the Q2 board deck from our Notion updates — 14 slides, one generated cover image.",
                "Scrape job postings from 20 competitor career pages every Monday and summarize the hiring signal.",
                "Review this 60-page MSA and flag anything non-standard against our template.",
                "Write a FastAPI service that ingests Stripe webhooks and writes to Supabase — with tests.",
              ].map((q) => (
                <div
                  key={q}
                  className="rounded-lg bg-card p-6 text-[14px] leading-[1.7] text-foreground ring-1 ring-border"
                >
                  <p className="text-muted-foreground">{q}</p>
                </div>
              ))}
            </div>
          </ScrollReveal>
        </div>
      </section>

      {/* Endurance — key differentiator */}
      <section className="mx-auto max-w-7xl px-6 py-16 md:py-32">
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
                analytical precision across sessions that run for hours,
                not minutes.
              </p>
              <p>
                A deep job can run for 12+ hours continuously: reading,
                writing code, testing hypotheses, iterating on the
                deliverable — all without losing track of what it's doing
                or why. The longer it runs, the deeper it gets.
              </p>
            </div>
          </ScrollReveal>
        </div>
      </section>

      {/* CTA */}
      <section className="bg-secondary/30">
        <div className="mx-auto max-w-7xl px-6 py-16 md:py-32">
          <ScrollReveal>
            <div className="max-w-xl">
              <h2 className="font-serif text-3xl font-semibold text-foreground md:text-4xl lg:text-[2.75rem]">
                Start with a single task.
              </h2>
              <p className="mt-4 text-[16px] leading-[1.7] text-muted-foreground">
                Every account includes credits to put Mariana to work
                today — no setup, no integrations required.
              </p>
              <div className="mt-8 flex flex-wrap items-center gap-5">
                <Link
                  to="/signup"
                  className="inline-flex items-center gap-2.5 rounded-md bg-primary px-6 py-3 text-sm font-medium text-primary-foreground transition-all hover:bg-primary/90 hover:shadow-xl hover:shadow-primary/10"
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
