import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { EmptyState } from "@/components/deft/states";
import { ArrowRight, Search } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

type Category =
  | "All"
  | "Operations"
  | "Research"
  | "Engineering"
  | "Marketing"
  | "Sales"
  | "Finance";

type Example = {
  title: string;
  prompt: string;
  outcome: string;
  deliverables: string[];
  runtime: string;
  category: Exclude<Category, "All">;
};

const categories: Category[] = [
  "All",
  "Operations",
  "Research",
  "Engineering",
  "Marketing",
  "Sales",
  "Finance",
];

// A curated catalog of tasks teams routinely hand to Deft. These are
// illustrative example prompts — click any of them to start a real task.
const examples: Example[] = [
  {
    title: "Weekly revenue and cohort report",
    prompt:
      "Pull last week's orders from our Postgres read-replica, compute WoW revenue, new vs returning split, and a 12-week cohort retention chart. Deliver a one-page PDF and post a Slack summary.",
    outcome:
      "Automated every Monday at 8am. Finance replaced a 3-hour manual roll-up.",
    deliverables: ["PDF report", "Charts", "Slack post", "Cron job"],
    runtime: "~12 min build, 45s each run",
    category: "Finance",
  },
  {
    title: "Internal admin dashboard",
    prompt:
      "Build an internal admin dashboard (React + Tailwind, Supabase auth) with tabs for Users, Billing, Feature Flags, and Audit Log. Deploy to a private URL.",
    outcome:
      "Operator tool delivered in a day. Replaced a Google Sheet and 5 Retool screens.",
    deliverables: [
      "Full React app",
      "Deployed URL",
      "Admin API routes",
      "README",
    ],
    runtime: "~2–4 hours",
    category: "Engineering",
  },
  {
    title: "Competitor teardown",
    prompt:
      "Give me a competitive teardown of the top 5 alternatives to our product. Pricing tiers, positioning, onboarding UX, recent releases, review sentiment. Output as a slide deck.",
    outcome:
      "20-slide deck with citations. PM team used it for Q2 planning.",
    deliverables: ["PPTX deck", "Cited sources", "Comparison table"],
    runtime: "~25 min",
    category: "Marketing",
  },
  {
    title: "Inbox triage & draft replies",
    prompt:
      "Every hour, read my support inbox, triage by severity, draft replies for tickets tagged 'billing' using our tone guide, and queue them for approval.",
    outcome:
      "CX team saves ~10 hrs/week. Drafts are approved in bulk.",
    deliverables: [
      "Scheduled job",
      "Triage rules",
      "Draft queue in Gmail",
    ],
    runtime: "Runs hourly",
    category: "Operations",
  },
  {
    title: "Lead enrichment for outbound",
    prompt:
      "Take this CSV of 500 leads, enrich with LinkedIn role, company size, funding, tech stack, and a personalized first line. Score and sort by fit. Drop back into the CSV.",
    outcome:
      "Enriched CSV back in 40 minutes. Outbound open rate jumped.",
    deliverables: ["Enriched CSV", "Personalized openers", "Fit score"],
    runtime: "~40 min for 500 rows",
    category: "Sales",
  },
  {
    title: "Landing page + analytics",
    prompt:
      "Build a landing page for our new SKU with a waitlist form, Stripe checkout, and PostHog events. Copy tone = confident but plain. Deploy to Vercel.",
    outcome:
      "Page live in under an hour. Collected 1.2k signups in week one.",
    deliverables: ["Live URL", "Stripe integration", "Analytics wired"],
    runtime: "~45 min",
    category: "Marketing",
  },
  {
    title: "API migration & regression suite",
    prompt:
      "Migrate our REST payments endpoints to v2 (idempotency keys, webhook signatures). Write a regression test suite, run it, fix whatever breaks.",
    outcome:
      "68 tests, 100% pass. Migration merged without a prod incident.",
    deliverables: ["Migrated code", "Test suite", "PR with review notes"],
    runtime: "~3 hours autonomous",
    category: "Engineering",
  },
  {
    title: "Board deck from the numbers",
    prompt:
      "Build next month's board deck. Pull ARR, NRR, burn, runway, hiring from our systems. Add narrative, highlight risks, cite assumptions. Output as PPTX.",
    outcome:
      "CFO edits a starting point instead of building from scratch.",
    deliverables: ["PPTX deck", "Backing model", "Commentary"],
    runtime: "~35 min",
    category: "Finance",
  },
  {
    title: "Market & pricing research",
    prompt:
      "Analyze the US small-business accounting software market. Size it, segment it, map competitors, identify unserved niches. Deliver a written report with sources.",
    outcome:
      "35-page report with 80+ citations. Informed the Q3 roadmap.",
    deliverables: ["PDF report", "Source bibliography", "Charts"],
    runtime: "~90 min",
    category: "Research",
  },
  {
    title: "Onboarding playbook generator",
    prompt:
      "For every new signup, generate a personalized 14-day onboarding email sequence based on their role and use case. Write copy, schedule in our ESP.",
    outcome:
      "Activation rate improved across every segment.",
    deliverables: [
      "Email sequences",
      "Scheduled sends",
      "Segmentation logic",
    ],
    runtime: "Runs on signup",
    category: "Marketing",
  },
  {
    title: "SOC 2 evidence collector",
    prompt:
      "Collect last quarter's access reviews, change logs, vendor reviews, and security training completion. Organize into the Vanta evidence format.",
    outcome:
      "Two weeks of audit prep collapsed into one evening.",
    deliverables: ["Evidence bundle", "Checklist status", "Gaps list"],
    runtime: "~2 hours",
    category: "Operations",
  },
  {
    title: "Pull, normalize, and chart public data",
    prompt:
      "Pull FDA medical device recalls for the last 5 years, normalize the classification field, chart recall counts by manufacturer, flag outliers.",
    outcome:
      "Clean dataset + chart-ready CSV. Used for a longer-form investigation.",
    deliverables: ["Clean CSV", "Charts", "Jupyter notebook"],
    runtime: "~50 min",
    category: "Research",
  },
];

export default function Research() {
  const [activeCat, setActiveCat] = useState<Category>("All");
  const filtered =
    activeCat === "All" ? examples : examples.filter((e) => e.category === activeCat);

  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <div className="mx-auto max-w-7xl px-6 pb-24 pt-32 md:pt-40">
        <ScrollReveal>
          <h1 className="font-serif text-3xl font-semibold leading-[1.08] tracking-[-0.02em] text-foreground sm:text-4xl md:text-5xl">
            Examples of things Deft has delivered
          </h1>
          <p className="mt-5 max-w-2xl text-lg leading-[1.7] text-muted-foreground">
            Real prompts, real deliverables. Click any example to run it against
            your own data and environment.
          </p>
        </ScrollReveal>

        {/* Category filter */}
        <div className="mt-10 flex flex-col gap-4 border-b border-border pb-4 sm:mt-12 sm:flex-row sm:flex-wrap sm:items-center sm:gap-3">
          <div className="flex gap-1 overflow-x-auto pb-1 -mx-1 px-1">
            {categories.map((c) => (
              <button
                key={c}
                onClick={() => setActiveCat(c)}
                className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                  activeCat === c
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:text-foreground hover:bg-secondary"
                }`}
              >
                {c}
              </button>
            ))}
          </div>
        </div>

        {/* Examples grid */}
        <div className="mt-10 grid gap-5 md:grid-cols-2">
          {filtered.map((ex, i) => (
            <ScrollReveal key={ex.title} delay={(i % 4) * 60}>
              <Link
                to={`/chat?prompt=${encodeURIComponent(ex.prompt)}`}
                className="group block h-full rounded-lg border border-border bg-card p-6 transition-all hover:border-accent/40 hover:shadow-lg"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1">
                    <div className="mb-2 flex flex-wrap items-center gap-2">
                      <span className="rounded-sm bg-secondary px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                        {ex.category}
                      </span>
                      <span className="font-mono text-[10px] text-muted-foreground">
                        {ex.runtime}
                      </span>
                    </div>
                    <h3 className="font-serif text-lg font-semibold text-foreground transition-colors group-hover:text-accent">
                      {ex.title}
                    </h3>
                  </div>
                  <ArrowRight
                    size={16}
                    className="mt-1 text-muted-foreground opacity-0 transition-all group-hover:translate-x-1 group-hover:opacity-100"
                  />
                </div>

                <p className="mt-4 text-[13px] italic leading-[1.7] text-muted-foreground">
                  "{ex.prompt}"
                </p>

                <p className="mt-4 text-[13px] leading-[1.7] text-foreground/80">
                  {ex.outcome}
                </p>

                <div className="mt-4 flex flex-wrap gap-1.5">
                  {ex.deliverables.map((d) => (
                    <span
                      key={d}
                      className="rounded-sm border border-border px-2 py-0.5 text-[10px] text-muted-foreground"
                    >
                      {d}
                    </span>
                  ))}
                </div>
              </Link>
            </ScrollReveal>
          ))}
        </div>

        {filtered.length === 0 && (
          <div className="mt-10">
            <EmptyState
              filtered
              icon={<Search size={20} aria-hidden="true" />}
              title="No examples in this category"
              description="Try another category, or describe what you have in mind — Deft will plan and run it from a fresh prompt."
              action={
                <button
                  type="button"
                  onClick={() => setActiveCat("All")}
                  className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  Show all examples
                </button>
              }
            />
          </div>
        )}

        {/* CTA */}
        <div className="mt-20 rounded-lg border border-border bg-secondary/30 p-8 md:p-12">
          <div className="max-w-2xl">
            <h2 className="font-serif text-2xl font-semibold text-foreground md:text-3xl">
              Have something specific in mind?
            </h2>
            <p className="mt-3 text-[15px] leading-[1.7] text-muted-foreground">
              Describe what you want built, researched, or automated. Deft
              plans the work, asks only what's necessary, then runs it end to end.
            </p>
            <Link
              to="/chat"
              className="mt-6 inline-flex items-center gap-2 rounded-md bg-primary px-5 py-2.5 text-sm font-medium text-primary-foreground transition-all hover:bg-primary/90"
            >
              Start a task <ArrowRight size={14} />
            </Link>
          </div>
        </div>
      </div>
      <Footer />
    </div>
  );
}
