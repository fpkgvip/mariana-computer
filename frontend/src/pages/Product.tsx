import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { usePageHead } from "@/lib/pageHead";

export default function Product() {
  usePageHead({
    title: "Product",
    description: "How Deft works: a real browser, a feedback loop that catches its own bugs, and code that actually runs the first time.",
    path: "/product",
  });
  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      {/* Hero */}
      <section className="mx-auto max-w-7xl px-6 pb-24 pt-32 md:pt-40">
        <ScrollReveal>
          <h1 className="max-w-4xl font-serif text-3xl font-semibold leading-[1.08] tracking-[-0.02em] text-foreground sm:text-4xl md:text-5xl lg:text-[4rem]">
            An AI developer with a real computer.
          </h1>
        </ScrollReveal>
        <ScrollReveal delay={150}>
          <p className="mt-8 max-w-xl text-lg leading-[1.7] text-muted-foreground">
            Deft writes the code, then runs the app in a real browser. It reads
            its own console, watches its own UI render, fixes its own errors,
            and only then hands you a live URL — not a to-do list.
          </p>
        </ScrollReveal>
        <ScrollReveal delay={300}>
          <div className="mt-10 flex flex-wrap items-center gap-5">
            <Link
              to="/signup"
              className="inline-flex items-center gap-2.5 rounded-md bg-primary px-6 py-3 text-sm font-medium text-primary-foreground transition-all hover:bg-primary/90 hover:shadow-xl hover:shadow-primary/10"
            >
              Try Deft free <ArrowRight size={15} />
            </Link>
            <Link
              to="/pricing"
              className="text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
            >
              View pricing →
            </Link>
          </div>
        </ScrollReveal>
      </section>

      {/* Computer control — the core differentiator */}
      <section className="bg-secondary/30">
        <div className="mx-auto max-w-7xl px-6 py-24 md:py-32">
          <div className="grid gap-20 lg:grid-cols-2">
            <div>
              <ScrollReveal>
                <h2 className="font-serif text-3xl font-semibold text-foreground md:text-4xl">
                  It has a computer. It uses it.
                </h2>
              </ScrollReveal>
              <ScrollReveal delay={100}>
                <div className="mt-8 space-y-6 text-[16px] leading-[1.8] text-muted-foreground">
                  <p>
                    When Deft needs information that isn't in a database, it
                    writes a program to get it — a browser script, an API
                    client, a parser for whatever file format it runs into. When
                    it needs to verify a claim, it builds a check and runs it.
                  </p>
                  <p>
                    It doesn't just produce text. It creates{" "}
                    <span className="text-foreground font-medium">whatever the work requires</span>:
                    Python and TypeScript programs, data pipelines, charts and
                    dashboards, PDFs, slide decks, spreadsheets, emails, and
                    complete web applications — deployed and linkable.
                  </p>
                  <p>
                    And it works autonomously. Close the tab. Deft keeps
                    running — executing steps, handling errors, pivoting, and
                    finishing the job. You get notified when it's done.
                  </p>
                </div>
              </ScrollReveal>
            </div>

            <ScrollReveal delay={250} className="self-start lg:mt-14">
              <div className="rounded-lg bg-card p-6 shadow-sm ring-1 ring-border">
                <p className="mb-5 text-[11px] font-medium uppercase tracking-[0.15em] text-muted-foreground">
                  What Deft delivers in a single task
                </p>
                <div className="space-y-4 text-[15px] leading-relaxed text-muted-foreground">
                  <p>
                    <span className="text-foreground font-medium">Programs.</span>{" "}
                    Custom scripts, API clients, data connectors, ETL pipelines
                    — written in Python or TypeScript, executed in a sandboxed
                    environment.
                  </p>
                  <p>
                    <span className="text-foreground font-medium">Analysis.</span>{" "}
                    Spreadsheets, statistical models, A/B test read-outs, cohort
                    breakdowns, forecasts — built from your actual data.
                  </p>
                  <p>
                    <span className="text-foreground font-medium">Applications.</span>{" "}
                    Dashboards, internal tools, marketing sites, small web apps
                    — deployed to a live URL you can share.
                  </p>
                  <p>
                    <span className="text-foreground font-medium">Documents.</span>{" "}
                    PDF reports, slide decks, Word docs, Excel workbooks,
                    emails, and SOPs — formatted and ready to send.
                  </p>
                </div>
              </div>
            </ScrollReveal>
          </div>
        </div>
      </section>

      {/* Adaptive intelligence */}
      <section className="mx-auto max-w-7xl px-6 py-24 md:py-32">
        <ScrollReveal>
          <h2 className="font-serif text-3xl font-semibold text-foreground md:text-4xl">
            Deft decides.
          </h2>
          <p className="mt-5 max-w-xl text-[16px] leading-[1.7] text-muted-foreground">
            You describe what you need. Deft picks the right approach —
            whether that's a ten-second lookup or a multi-hour build.
          </p>
        </ScrollReveal>
        <div className="mt-14 grid gap-6 sm:grid-cols-3">
          {[
            {
              label: "Instant",
              time: "Seconds to minutes",
              desc: "Quick answers, fast lookups, single-file edits, one-shot scripts. No approval needed — Deft answers immediately.",
            },
            {
              label: "Standard",
              time: "Minutes to hours",
              desc: "Multi-step work: reports, dashboards, small apps, data pipelines, document generation. Deft proposes a plan before it starts.",
            },
            {
              label: "Deep",
              time: "Hours to days",
              desc: "Long-running autonomous work: full applications, large research projects, complex migrations, end-to-end automations.",
            },
          ].map((tier, i) => (
            <ScrollReveal key={tier.label} delay={i * 80}>
              <div className="rounded-lg bg-card p-6 shadow-sm ring-1 ring-border h-full">
                <h3 className="text-[15px] font-semibold text-foreground">{tier.label}</h3>
                <p className="mt-1 font-mono text-xs text-accent-strong">{tier.time}</p>
                <p className="mt-3 text-sm leading-[1.7] text-muted-foreground">{tier.desc}</p>
              </div>
            </ScrollReveal>
          ))}
        </div>
      </section>

      {/* What it actually ships */}
      <section className="bg-secondary/30">
        <div className="mx-auto max-w-7xl px-6 py-24 md:py-32">
          <ScrollReveal>
            <h2 className="max-w-2xl font-serif text-3xl font-semibold text-foreground md:text-4xl">
              What it actually ships
            </h2>
            <p className="mt-5 max-w-2xl text-[16px] leading-[1.7] text-muted-foreground">
              Deft doesn't stop at a draft. It runs the code, verifies the
              output, fixes its own mistakes, and hands you something you can
              use today — not a to-do list.
            </p>
          </ScrollReveal>
          <div className="mt-14 grid gap-10 md:grid-cols-2">
            {[
              {
                title: "Working software",
                desc: "Internal tools, admin dashboards, scheduled jobs, Slack bots, Chrome extensions, landing pages — built, tested, and deployed to a live URL.",
              },
              {
                title: "Real analysis on real data",
                desc: "Pull from your warehouse, spreadsheet, or an API. Clean it, model it, chart it, write it up. Numbers that match your source, not made-up estimates.",
              },
              {
                title: "Polished documents",
                desc: "Board decks, customer proposals, research reports, product specs, policies, onboarding docs — in PDF, DOCX, PPTX, or XLSX, with citations where it matters.",
              },
              {
                title: "Automations that run",
                desc: "Recurring reports, inbox triage, CRM hygiene, monitoring jobs — scheduled, running, and sending you the results. Not a workflow diagram.",
              },
            ].map((item, i) => (
              <ScrollReveal key={item.title} delay={i * 100}>
                <div className="border-l-2 border-accent/40 pl-6 py-1">
                  <h3 className="text-[15px] font-semibold text-foreground">
                    {item.title}
                  </h3>
                  <p className="mt-2 text-[15px] leading-[1.8] text-muted-foreground">
                    {item.desc}
                  </p>
                </div>
              </ScrollReveal>
            ))}
          </div>
        </div>
      </section>

      {/* How it thinks — frontier models */}
      <section className="mx-auto max-w-7xl px-6 py-24 md:py-32">
        <ScrollReveal>
          <h2 className="font-serif text-3xl font-semibold text-foreground md:text-4xl">
            Frontier reasoning, end-to-end execution
          </h2>
        </ScrollReveal>
        <div className="mt-12 grid gap-20 lg:grid-cols-2">
          <ScrollReveal delay={100}>
            <div className="space-y-6 text-[16px] leading-[1.8] text-muted-foreground">
              <p>
                Deft is powered by frontier models — Claude, GPT, Gemini, and
                specialized coding and vision models — routed automatically for
                each step. It doesn't just run one prompt and return one
                response. It plans, acts, checks, and revises like a good
                contractor would.
              </p>
              <p>
                When it hits a gap — missing data, a failing test, an API that
                moved — it doesn't bluff. It writes a program to close the gap,
                runs it, verifies the result, and only then moves on. Every
                step is logged and inspectable.
              </p>
            </div>
          </ScrollReveal>
          <ScrollReveal delay={250}>
            <div className="space-y-6 text-[16px] leading-[1.8] text-muted-foreground">
              <p>
                The depth is the point. A one-paragraph summary is fast.
                Shipping a working tool, cleaning a real dataset, producing a
                numbers-accurate report — that takes real compute time, and
                Deft will use it.
              </p>
              <p>
                Some jobs finish in seconds. Others run for hours or days —
                writing thousands of lines of code, making thousands of API
                calls, building and testing until the work is actually done.
              </p>
            </div>
          </ScrollReveal>
        </div>
      </section>

      {/* Built for teams */}
      <section className="bg-secondary/30">
        <div className="mx-auto max-w-7xl px-6 py-24 md:py-32">
          <ScrollReveal>
            <h2 className="font-serif text-3xl font-semibold text-foreground md:text-4xl">
              Built for teams that need code that actually runs
            </h2>
          </ScrollReveal>
          <div className="mt-14 grid gap-12 sm:grid-cols-2 md:grid-cols-3">
            {[
              {
                title: "Operators & Founders",
                items: [
                  "Spin up internal tools and admin panels in a day",
                  "Automate weekly reporting, inbox triage, CRM hygiene",
                  "Turn a Loom-level idea into a working prototype",
                ],
              },
              {
                title: "Product & Engineering",
                items: [
                  "Feature spikes, migrations, and throwaway scripts",
                  "Generate SDKs, docs, and test harnesses from an API",
                  "Run full build/test/deploy loops autonomously",
                ],
              },
              {
                title: "Analysts & Researchers",
                items: [
                  "Clean and model data from warehouses, sheets, and APIs",
                  "Produce board decks, reports, and dashboards on demand",
                  "Reproduce the exact pipeline every week, every month",
                ],
              },
              {
                title: "Marketing & Growth",
                items: [
                  "Landing pages, campaigns, and tracking plans",
                  "Competitor sweeps, SEO audits, pricing teardowns",
                  "Generate launch assets in every format you need",
                ],
              },
              {
                title: "Sales & Success",
                items: [
                  "Account research packs and custom outreach at scale",
                  "Auto-generated proposals, SOWs, and renewal briefs",
                  "QBR decks pulled straight from CRM data",
                ],
              },
              {
                title: "Finance & Ops",
                items: [
                  "Close-cycle automation and reconciliation checks",
                  "Variance analysis, cohort reporting, forecast updates",
                  "One-off deep dives that would normally take a week",
                ],
              },
            ].map((uc, i) => (
              <ScrollReveal key={uc.title} delay={i * 120}>
                <div className="border-l-2 border-accent/40 pl-6">
                  <h3 className="mb-4 text-sm font-semibold uppercase tracking-wider text-foreground">
                    {uc.title}
                  </h3>
                  <ul className="space-y-2.5">
                    {uc.items.map((item) => (
                      <li key={item} className="text-sm leading-relaxed text-muted-foreground">
                        {item}
                      </li>
                    ))}
                  </ul>
                </div>
              </ScrollReveal>
            ))}
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="mx-auto max-w-7xl px-6 py-24 md:py-32">
        <ScrollReveal>
          <div className="max-w-xl">
            <h2 className="font-serif text-3xl font-semibold text-foreground md:text-4xl">
              Give it a real job.
            </h2>
            <p className="mt-4 text-[16px] leading-[1.7] text-muted-foreground">
              Describe something you{"\u2019"}ve been meaning to deliver. Deft
              plans it, writes it, runs it, and hands it back to you — already
              working.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-5">
              <Link
                to="/signup"
                className="inline-flex items-center gap-2.5 rounded-md bg-primary px-6 py-3 text-sm font-medium text-primary-foreground transition-all hover:bg-primary/90 hover:shadow-xl hover:shadow-primary/10"
              >
                Start your first task <ArrowRight size={15} />
              </Link>
              <Link
                to="/pricing"
                className="text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
              >
                View plans →
              </Link>
            </div>
          </div>
        </ScrollReveal>
      </section>

      <Footer />
    </div>
  );
}
