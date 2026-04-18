import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";

export default function Mariana() {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      {/* Hero */}
      <section className="mx-auto max-w-7xl px-6 pb-20 pt-32 md:pt-40">
        <ScrollReveal>
          <h1 className="max-w-4xl text-3xl font-bold leading-[1.08] tracking-tight text-foreground sm:text-4xl md:text-5xl lg:text-6xl">
            An AI with its own computer.
          </h1>
        </ScrollReveal>
        <ScrollReveal delay={120}>
          <p className="mt-6 max-w-xl text-lg leading-relaxed text-muted-foreground">
            Mariana operates inside a full compute environment. It writes and
            runs code, builds applications, queries live data sources, and works
            autonomously until the investigation is complete.
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
              to="/pricing"
              className="text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
            >
              View pricing →
            </Link>
          </div>
        </ScrollReveal>
      </section>

      {/* Computer control */}
      <section className="border-y border-border bg-card/30">
        <div className="mx-auto max-w-7xl px-6 py-20 md:py-32">
          <div className="grid gap-16 lg:grid-cols-2">
            <div>
              <ScrollReveal>
                <h2 className="text-3xl font-bold text-foreground md:text-4xl">
                  It has a computer. It uses it.
                </h2>
              </ScrollReveal>
              <ScrollReveal delay={100}>
                <div className="mt-8 space-y-5 text-base leading-relaxed text-muted-foreground">
                  <p>
                    When Mariana needs data that doesn't exist in a database, it
                    writes a program to get it — a web program, an API client, a
                    parser for obscure file formats. When it needs to test a
                    hypothesis, it builds a statistical model and runs it.
                  </p>
                  <p>
                    It doesn't just produce text. It creates{" "}
                    <span className="font-semibold text-foreground">whatever the research requires</span>:
                    Python scripts, Monte Carlo simulations, regression models,
                    custom data programs, data pipelines, interactive dashboards,
                    finished PDF reports, slide decks, even full web applications.
                  </p>
                  <p>
                    And it works autonomously. Close your browser. Mariana keeps
                    running — following evidence chains, hitting dead ends,
                    pivoting, trying new approaches. You get notified when it's done.
                  </p>
                </div>
              </ScrollReveal>
            </div>

            <ScrollReveal delay={200} className="self-start lg:mt-8">
              <div className="rounded-xl border border-border bg-card p-6 shadow-sm">
                <p className="mb-4 text-xs font-semibold uppercase tracking-widest text-muted-foreground">
                  What Mariana builds during a single investigation
                </p>
                <div className="space-y-4 text-sm leading-relaxed text-muted-foreground">
                  <p>
                    <span className="font-semibold text-foreground">Programs.</span>{" "}
                    Custom data programs, API clients, data parsers, ETL pipelines —
                    written in Python, executed in a sandboxed environment.
                  </p>
                  <p>
                    <span className="font-semibold text-foreground">Models.</span>{" "}
                    Monte Carlo simulations, regression analyses, NLP classifiers,
                    factor models — built from scratch for each query.
                  </p>
                  <p>
                    <span className="font-semibold text-foreground">Applications.</span>{" "}
                    Interactive dashboards, monitoring systems, custom web apps,
                    data visualization tools — deployed and ready to use.
                  </p>
                  <p>
                    <span className="font-semibold text-foreground">Documents.</span>{" "}
                    PDF reports, slide decks, Excel workbooks, data exports —
                    formatted and presentation-ready.
                  </p>
                </div>
              </div>
            </ScrollReveal>
          </div>
        </div>
      </section>

      {/* Research depth tiers */}
      <section className="mx-auto max-w-7xl px-6 py-20 md:py-32">
        <ScrollReveal>
          <h2 className="text-3xl font-bold text-foreground md:text-4xl">
            Mariana decides.
          </h2>
          <p className="mt-4 max-w-xl text-base leading-relaxed text-muted-foreground">
            You describe what you need. Mariana determines the right approach —
            whether that's a quick lookup or a week-long investigation.
          </p>
        </ScrollReveal>
        <div className="mt-12 grid gap-4 sm:grid-cols-3">
          {[
            {
              label: "Instant",
              time: "Seconds to minutes",
              desc: "Factual questions, quick data lookups, and targeted single-source retrievals. No approval needed — Mariana answers immediately.",
            },
            {
              label: "Standard",
              time: "Minutes to hours",
              desc: "Multi-source research with cross-referencing, model building, and comprehensive analysis. Mariana proposes a plan for your approval before starting.",
            },
            {
              label: "Deep",
              time: "Hours to days",
              desc: "Exhaustive autonomous investigations across jurisdictions — writing custom tooling, testing hypotheses, and producing institutional-grade deliverables.",
            },
          ].map((tier, i) => (
            <ScrollReveal key={tier.label} delay={i * 80}>
              <div className="h-full rounded-xl border border-border bg-card p-6 shadow-sm transition-all hover:border-primary/20 hover:shadow-md">
                <h3 className="text-sm font-bold text-foreground">{tier.label}</h3>
                <p className="mt-1 font-mono text-xs text-primary">{tier.time}</p>
                <p className="mt-3 text-sm leading-relaxed text-muted-foreground">{tier.desc}</p>
              </div>
            </ScrollReveal>
          ))}
        </div>
      </section>

      {/* What it finds */}
      <section className="border-y border-border bg-card/30">
        <div className="mx-auto max-w-7xl px-6 py-20 md:py-32">
          <ScrollReveal>
            <h2 className="max-w-2xl text-3xl font-bold text-foreground md:text-4xl">
              What it finds that nobody else catches
            </h2>
            <p className="mt-4 max-w-2xl text-base leading-relaxed text-muted-foreground">
              Mariana doesn't summarize public information. It investigates —
              reading raw filings, cross-referencing datasets across jurisdictions,
              and building custom tools to test hypotheses no one has thought to test.
            </p>
          </ScrollReveal>
          <div className="mt-12 grid gap-8 md:grid-cols-2">
            {[
              {
                title: "Accounting irregularities",
                desc: "Revenue recognition timing shifts, off-balance-sheet structures, related-party transactions buried in footnotes across years of filings. Mariana reads every page — not summaries.",
              },
              {
                title: "Hidden corporate relationships",
                desc: "Shared directors between a company and its suppliers. Subsidiary structures designed to obscure ownership. Undisclosed arrangements that materially affect margins.",
              },
              {
                title: "Unusual trading patterns",
                desc: "Statistically anomalous insider transactions timed around unreported events. Options activity clustering. Share accumulation patterns that precede material disclosures.",
              },
              {
                title: "Supply chain exposure",
                desc: "Single-source dependencies, geographic concentration risk, undisclosed supplier relationships — mapped by cross-referencing trade data, corporate registries, and procurement filings.",
              },
            ].map((item, i) => (
              <ScrollReveal key={item.title} delay={i * 80}>
                <div className="rounded-xl border border-border bg-card p-6 transition-all hover:border-primary/20 hover:shadow-sm">
                  <h3 className="text-sm font-bold text-foreground">
                    {item.title}
                  </h3>
                  <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                    {item.desc}
                  </p>
                </div>
              </ScrollReveal>
            ))}
          </div>
        </div>
      </section>

      {/* Frontier reasoning */}
      <section className="mx-auto max-w-7xl px-6 py-20 md:py-32">
        <ScrollReveal>
          <h2 className="text-3xl font-bold text-foreground md:text-4xl">
            Frontier reasoning, not surface analysis
          </h2>
        </ScrollReveal>
        <div className="mt-10 grid gap-12 lg:grid-cols-2">
          <ScrollReveal delay={100}>
            <div className="space-y-5 text-base leading-relaxed text-muted-foreground">
              <p>
                Powered by Claude Opus and other frontier models capable of
                sustained, multi-step reasoning. Mariana doesn't run a prompt
                and return a response. It reasons through problems the way a
                research team would: formulating hypotheses, gathering evidence,
                testing assumptions, revising conclusions, and going deeper when
                something doesn't add up.
              </p>
              <p>
                When it hits a gap in available data, it doesn't approximate.
                It writes a program to go get the data — gathering trade
                registries, parsing PDF exhibits, calling financial data APIs,
                querying SEC EDGAR directly. Then it validates what it found
                against multiple independent sources before drawing conclusions.
              </p>
            </div>
          </ScrollReveal>
          <ScrollReveal delay={200}>
            <div className="space-y-5 text-base leading-relaxed text-muted-foreground">
              <p>
                The depth is the point. A surface-level scan of a company
                might take minutes. Tracing an undisclosed related-party
                transaction through three jurisdictions, building a custom
                pricing model to quantify its impact, and stress-testing that
                model against macro scenarios — that takes real compute time.
              </p>
              <p>
                Mariana will work for as long as the investigation demands.
                Some queries resolve in minutes. Others require hours or days of
                autonomous operation — writing thousands of lines of code,
                making thousands of API calls, building and discarding models
                until the evidence is clear.
              </p>
            </div>
          </ScrollReveal>
        </div>
      </section>

      {/* Built for teams */}
      <section className="border-y border-border bg-card/30">
        <div className="mx-auto max-w-7xl px-6 py-20 md:py-32">
          <ScrollReveal>
            <h2 className="text-3xl font-bold text-foreground md:text-4xl">
              Built for teams that need depth
            </h2>
          </ScrollReveal>
          <div className="mt-12 grid gap-6 sm:grid-cols-2 md:grid-cols-3">
            {[
              {
                title: "Quantitative Funds",
                items: [
                  "Factor exposure decomposition across portfolios",
                  "Alternative data signal validation and backtesting",
                  "Custom model construction and stress testing",
                ],
              },
              {
                title: "Hedge Funds",
                items: [
                  "Short thesis development with primary source verification",
                  "Event-driven research (M&A, activism, restructuring)",
                  "Supply chain mapping and concentration risk analysis",
                ],
              },
              {
                title: "Institutional Research",
                items: [
                  "Sector-wide competitive landscape analysis",
                  "Regulatory impact modeling across jurisdictions",
                  "Cross-border corporate structure investigation",
                ],
              },
            ].map((uc, i) => (
              <ScrollReveal key={uc.title} delay={i * 100}>
                <div className="rounded-xl border border-border bg-card p-6 h-full">
                  <h3 className="mb-4 text-xs font-bold uppercase tracking-wider text-primary">
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
      <section className="mx-auto max-w-7xl px-6 py-20 md:py-32">
        <ScrollReveal>
          <div className="max-w-xl">
            <h2 className="text-3xl font-bold text-foreground md:text-4xl">
              Give it a real problem.
            </h2>
            <p className="mt-4 text-base leading-relaxed text-muted-foreground">
              Start your first investigation today. If the depth is what your team needs,
              we'll structure a plan that fits.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-4">
              <Link
                to="/chat"
                className="inline-flex items-center gap-2 rounded-lg bg-primary px-6 py-3 text-sm font-semibold text-primary-foreground shadow-md transition-all hover:opacity-90 hover:shadow-lg"
              >
                Start investigating <ArrowRight size={15} />
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
