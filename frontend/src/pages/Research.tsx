import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Lock, ArrowRight } from "lucide-react";
import { useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Link } from "react-router-dom";

type AccessLevel = "all" | "free" | "premium";

const sectors = ["All", "Macro", "Equities", "Fixed Income", "Commodities", "Crypto"];

const reports = [
  {
    title: "The Semiconductor Supercycle: A 10-Year DCF Framework",
    abstract: "A comprehensive discounted cash flow analysis across 23 semiconductor firms, modeling capex cycles, geopolitical risk premia, and demand elasticity under three macro scenarios.",
    date: "2026-04-08",
    readTime: "42 min",
    sector: "Equities",
    access: "premium" as const,
  },
  {
    title: "US Treasury Yield Curve Dynamics Under QT",
    abstract: "Quantitative analysis of yield curve shape evolution during the current quantitative tightening cycle, with comparisons to 2018-2019 and historical precedents.",
    date: "2026-04-05",
    readTime: "28 min",
    sector: "Fixed Income",
    access: "free" as const,
  },
  {
    title: "Global Macro Outlook: Q2 2026",
    abstract: "Cross-asset macro framework incorporating monetary policy divergence, fiscal trajectories, and geopolitical risk premia across G10 and major EM economies.",
    date: "2026-04-01",
    readTime: "35 min",
    sector: "Macro",
    access: "free" as const,
  },
  {
    title: "Crude Oil: Monte Carlo Supply Disruption Modeling",
    abstract: "50,000-iteration Monte Carlo simulation modeling OPEC+ compliance variance, Strait of Hormuz disruption probability, and US shale production elasticity on WTI forward curves.",
    date: "2026-03-28",
    readTime: "55 min",
    sector: "Commodities",
    access: "premium" as const,
  },
  {
    title: "Ethereum Post-Dencun: Protocol Revenue & Valuation",
    abstract: "On-chain data analysis of Ethereum's fee market post-Dencun upgrade, modeling L2 settlement revenue, blob fee economics, and ETH as a productive asset.",
    date: "2026-03-22",
    readTime: "30 min",
    sector: "Crypto",
    access: "premium" as const,
  },
  {
    title: "Japanese Yen Carry Trade: Unwind Risk Assessment",
    abstract: "Sizing the global yen carry trade using BIS data, modeling unwind scenarios triggered by BOJ policy normalization, and second-order effects on US credit markets.",
    date: "2026-03-18",
    readTime: "24 min",
    sector: "Macro",
    access: "free" as const,
  },
];

export default function Research() {
  const { user } = useAuth();
  const [accessFilter, setAccessFilter] = useState<AccessLevel>("all");
  const [sectorFilter, setSectorFilter] = useState("All");

  const filtered = reports.filter((r) => {
    if (accessFilter !== "all" && r.access !== accessFilter) return false;
    if (sectorFilter !== "All" && r.sector !== sectorFilter) return false;
    return true;
  });

  const canAccessPremium = user && user.tokens > 0;

  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <div className="mx-auto max-w-7xl px-6 pb-24 pt-32 md:pt-40">
        <ScrollReveal>
          <h1 className="font-serif text-3xl font-semibold leading-[1.08] tracking-[-0.02em] text-foreground sm:text-4xl md:text-5xl">
            Published Research
          </h1>
          <p className="mt-5 max-w-lg text-lg leading-[1.7] text-muted-foreground">
            Institutional-quality analysis across asset classes. Select reports
            are free — premium research requires tokens.
          </p>
        </ScrollReveal>

        {/* Filters */}
        <div className="mt-10 flex flex-col gap-4 border-b border-border pb-4 sm:mt-12 sm:flex-row sm:flex-wrap sm:items-center sm:gap-6">
          <div className="flex gap-1">
            {(["all", "free", "premium"] as AccessLevel[]).map((level) => (
              <button
                key={level}
                onClick={() => setAccessFilter(level)}
                className={`rounded-md px-3 py-1.5 text-xs font-medium capitalize transition-colors ${
                  accessFilter === level
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:text-foreground hover:bg-secondary"
                }`}
              >
                {level}
              </button>
            ))}
          </div>
          <div className="hidden h-4 w-px bg-border sm:block" />
          <div className="flex gap-1 overflow-x-auto pb-1 -mx-1 px-1">
            {sectors.map((s) => (
              <button
                key={s}
                onClick={() => setSectorFilter(s)}
                className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                  sectorFilter === s
                    ? "bg-secondary text-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>

        {/* Reports */}
        <div className="mt-8 divide-y divide-border">
          {filtered.map((report, i) => (
            <ScrollReveal key={i} delay={i * 60}>
              {/* BUG-021: Articles are not clickable — removed cursor-pointer and hover arrow
                  to avoid implying navigation that doesn't exist yet.
                  TODO: wrap in <Link> once report detail pages are added. */}
              <article className="group relative py-8 transition-colors">
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1">
                    <div className="mb-2.5 flex flex-wrap items-center gap-3">
                      <span className="font-mono text-[11px] text-muted-foreground">
                        {report.date}
                      </span>
                      <span className="text-[11px] text-muted-foreground">
                        {report.readTime} read
                      </span>
                      <span className="text-[11px] text-muted-foreground/60">
                        {report.sector}
                      </span>
                      {report.access === "premium" ? (
                        <span className="inline-flex items-center gap-1 text-[11px] font-medium text-accent">
                          <Lock size={10} /> Premium
                        </span>
                      ) : (
                        <span className="text-[11px] text-muted-foreground">Free</span>
                      )}
                    </div>
                    <h2 className="font-serif text-xl font-semibold text-foreground transition-colors group-hover:text-accent">
                      {report.title}
                    </h2>
                    <p className="mt-2 max-w-2xl text-sm leading-[1.7] text-muted-foreground">
                      {report.abstract}
                    </p>
                  </div>
                  {/* ArrowRight removed — articles not yet clickable */}
                </div>

                {/* Paywall overlay */}
                {report.access === "premium" && !canAccessPremium && (
                  <div className="absolute inset-0 flex items-center justify-center rounded-lg bg-background/80 backdrop-blur-sm">
                    <div className="text-center">
                      <Lock size={18} className="mx-auto mb-2 text-accent" />
                      <p className="text-sm font-medium text-foreground">Premium Research</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {user ? "Purchase tokens to access" : "Sign in to access"}
                      </p>
                      <Link
                        to={user ? "/pricing" : "/login"}
                        className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
                      >
                        {user ? "Get Tokens" : "Sign In"} <ArrowRight size={12} />
                      </Link>
                    </div>
                  </div>
                )}
              </article>
            </ScrollReveal>
          ))}
        </div>

        {filtered.length === 0 && (
          <p className="py-12 text-center text-muted-foreground">
            No reports match the current filters.
          </p>
        )}
      </div>
      <Footer />
    </div>
  );
}
