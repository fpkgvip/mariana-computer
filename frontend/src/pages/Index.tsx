import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { Link, useNavigate } from "react-router-dom";
import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowUpRight, Sparkles, Eye, Globe } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { BRAND, STORAGE } from "@/lib/brand";

/**
 * Deft homepage — prompt-first.
 *
 * The hero IS the input.  The thesis (locked in
 * docs/positioning/phase_01_positioning.md) sits above it: "The AI developer
 * that doesn't leave you debugging."  Submitting branches on auth: a logged-in
 * user goes straight into /build with the prompt prefilled, an unauthenticated
 * visitor goes to /signup with the prompt preserved through the round-trip.
 */

const CYCLING_PROMPTS = [
  "Build a habit tracker with a streak heatmap and Supabase auth.",
  "A landing page for a SaaS that books vet appointments — dark, minimal, with a pricing table.",
  "Internal dashboard that pulls Stripe charges and shows MRR by plan, refreshed nightly.",
  "Two-player chess against an AI, with rated ELO and a leaderboard.",
  "A Hacker News–style forum for indie game devs, with markdown comments.",
  "A pixel-perfect clone of Linear's marketing site, but for a project called \"Quill\".",
  "An AI flashcard app that turns a PDF into a Quizlet-style deck.",
  "A booking page for a sushi restaurant — calendar, deposits, SMS confirms.",
];

// Five-stage pipeline.  These are descriptive technical steps, not marketing
// verbs.  The last stage is "Live" — a live URL is the receipt.
const STAGES = [
  { label: "Plan", caption: "Break the goal into ordered steps." },
  { label: "Write", caption: "Generate every file." },
  { label: "Compile", caption: "Build, lint, type-check." },
  { label: "Verify", caption: "Open it in a real browser. Catch its own errors." },
  { label: "Live", caption: "Push to a public URL." },
];

export default function Index() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const [prompt, setPrompt] = useState("");
  const [phIndex, setPhIndex] = useState(0);
  const [phText, setPhText] = useState("");
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  // Type-on / type-off rotating placeholder. Pauses while the user is typing.
  useEffect(() => {
    if (prompt.length > 0) return;
    const target = CYCLING_PROMPTS[phIndex];
    let cancelled = false;
    let i = 0;
    setPhText("");
    const tick = () => {
      if (cancelled) return;
      if (i <= target.length) {
        setPhText(target.slice(0, i));
        i += 1;
        setTimeout(tick, 22 + Math.random() * 18);
      } else {
        setTimeout(() => {
          if (cancelled) return;
          setPhIndex((x) => (x + 1) % CYCLING_PROMPTS.length);
        }, 2400);
      }
    };
    tick();
    return () => {
      cancelled = true;
    };
  }, [phIndex, prompt.length]);

  // Auto-grow textarea
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [prompt]);

  const submit = (text?: string) => {
    const value = (text ?? prompt).trim();
    if (!value) {
      inputRef.current?.focus();
      return;
    }
    try {
      sessionStorage.setItem(STORAGE.recentPrompts, JSON.stringify({ prompt: value, ts: Date.now() }));
    } catch {
      /* no-op: storage may be unavailable in private mode */
    }
    // Auth-aware routing.  An authenticated user lands directly in the
    // studio with the prompt prefilled; an anonymous visitor goes to
    // /signup, which forwards them to /build?prompt=... after they create
    // an account.  We never push an unauth user into /build, since the
    // ProtectedRoute bounce-to-login would silently drop the prompt.
    const buildHref = `/build?prompt=${encodeURIComponent(value)}`;
    if (user) {
      navigate(buildHref);
    } else {
      navigate(`/signup?next=${encodeURIComponent(buildHref)}`);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      submit();
    } else if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const exampleChips = useMemo(
    () => [
      "A pomodoro timer that rewards me with cat gifs",
      "Real-time poll app with QR-code joining",
      "Personal finance tracker with CSV import",
      "AI résumé builder, exports to PDF",
    ],
    [],
  );

  return (
    <div className="relative min-h-screen overflow-hidden bg-background text-foreground">
      <Navbar />

      {/* HERO — the prompt is the hero. */}
      <section className="relative isolate flex min-h-[100svh] items-center pt-24 pb-20">
        {/* Backdrops: subtle grid + vignette + a single soft accent halo. */}
        <div className="absolute inset-0 -z-10 bg-grid opacity-[0.55]" aria-hidden />
        <div className="absolute inset-0 -z-10 bg-vignette" aria-hidden />
        <div
          className="absolute left-1/2 top-[34%] -z-10 h-[520px] w-[820px] -translate-x-1/2 -translate-y-1/2 rounded-full opacity-[0.18] blur-3xl"
          style={{ background: "radial-gradient(closest-side, hsl(var(--accent) / 0.55), transparent)" }}
          aria-hidden
        />

        <div className="container-deft w-full">
          <div className="mx-auto max-w-[940px] text-center">
            {/* Eyebrow */}
            <div className="mx-auto mb-7 inline-flex items-center gap-2 rounded-full border border-border/70 bg-surface-1/60 px-3 py-1 text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground backdrop-blur">
              <span className="size-1.5 rounded-full bg-deploy animate-pulse" />
              The AI developer with a real computer
            </div>

            {/* Headline — locked thesis (Phase 01 positioning) */}
            <h1 className="text-balance text-[44px] font-semibold leading-[1.02] tracking-[-0.035em] sm:text-6xl md:text-[80px] lg:text-[88px]">
              The AI developer
              <br className="hidden sm:block" />{" "}
              <span className="text-muted-foreground">that doesn{"\u2019"}t leave you </span>
              <span className="relative inline-block">
                <span className="relative z-10">debugging</span>
                <span
                  className="absolute inset-x-0 bottom-[0.10em] -z-0 h-[0.18em] rounded-full"
                  style={{ background: "hsl(var(--deploy) / 0.55)" }}
                  aria-hidden
                />
              </span>
              <span className="text-muted-foreground">.</span>
            </h1>

            {/* Sub — three-line thesis, condensed */}
            <p className="mx-auto mt-6 max-w-[680px] text-pretty text-[16px] leading-[1.6] text-ink-1 sm:text-lg">
              {BRAND.name} runs your app in a real browser, watches its own output,
              and fixes its own mistakes — before you see them. You describe what
              you want. {BRAND.name} delivers software that runs.
            </p>

            {/* The Prompt — the actual hero. */}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                submit();
              }}
              className="group mx-auto mt-12 w-full max-w-[760px]"
            >
              <div
                className={[
                  "relative rounded-2xl border bg-surface-1/80 backdrop-blur-md",
                  "border-border/80 shadow-elev-2 transition-all duration-200",
                  "focus-within:border-accent/60 focus-within:shadow-[0_0_0_4px_hsl(var(--accent)/0.10),0_18px_48px_-22px_hsl(var(--accent)/0.55)]",
                ].join(" ")}
              >
                <textarea
                  ref={inputRef}
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  onKeyDown={onKeyDown}
                  rows={1}
                  spellCheck={false}
                  aria-label="Describe what you want to build"
                  placeholder=" "
                  className={[
                    "block w-full resize-none bg-transparent",
                    "px-5 pt-[18px] pb-[60px] text-left text-[17px] leading-[1.55] text-foreground",
                    "placeholder-transparent outline-none",
                  ].join(" ")}
                />
                {/* Cycling placeholder (overlay so we control the caret + animation precisely) */}
                {prompt.length === 0 && (
                  <div
                    className="pointer-events-none absolute left-5 right-20 top-[18px] truncate text-left text-[17px] leading-[1.55] text-muted-foreground"
                    aria-hidden
                  >
                    {phText}
                    <span className="deft-caret ml-0.5 align-baseline" />
                  </div>
                )}

                {/* Toolbar */}
                <div className="absolute inset-x-3 bottom-2 flex items-center justify-between">
                  <div className="flex items-center gap-1.5 pl-2 text-[11px] text-muted-foreground/80">
                    <Sparkles size={12} className="text-accent" />
                    <span>Press Enter to start</span>
                  </div>
                  <button
                    type="submit"
                    aria-label="Start"
                    className={[
                      "inline-flex h-9 items-center gap-1.5 rounded-lg px-3.5 text-[13px] font-medium",
                      "transition-all duration-150",
                      prompt.trim().length > 0
                        ? "bg-accent text-accent-foreground shadow-[0_4px_16px_-6px_hsl(var(--accent)/0.6)] hover:brightness-110"
                        : "bg-surface-3 text-muted-foreground hover:text-foreground",
                    ].join(" ")}
                  >
                    Start
                    <ArrowUpRight size={14} />
                  </button>
                </div>
              </div>

              {/* Example chips */}
              <div className="mt-5 flex flex-wrap items-center justify-center gap-2 text-[12px]">
                <span className="text-muted-foreground/70">Try</span>
                {exampleChips.map((c) => (
                  <button
                    key={c}
                    type="button"
                    onClick={() => {
                      setPrompt(c);
                      requestAnimationFrame(() => inputRef.current?.focus());
                    }}
                    className="rounded-full border border-border/60 bg-surface-1/70 px-3 py-1 text-muted-foreground transition-all hover:border-accent/50 hover:bg-surface-2 hover:text-foreground"
                  >
                    {c}
                  </button>
                ))}
              </div>
            </form>

            {/* Trust strip — three concrete behaviors, not defensive badges */}
            <div className="mt-14 flex flex-wrap items-center justify-center gap-x-8 gap-y-3 text-[11px] uppercase tracking-[0.14em] text-muted-foreground/80">
              <span className="inline-flex items-center gap-1.5"><Eye size={13} className="text-accent" /> Runs in a real browser</span>
              <span className="inline-flex items-center gap-1.5"><Sparkles size={13} className="text-deploy" /> Catches its own bugs</span>
              <span className="inline-flex items-center gap-1.5"><Globe size={13} className="text-accent" /> Live preview URL</span>
            </div>
          </div>
        </div>
      </section>

      {/* THE LOOP — Plan → Write → Compile → Verify → Live */}
      <section className="relative border-t border-border/60 bg-surface-1/40">
        <div className="container-deft py-24 md:py-32">
          <div className="mx-auto max-w-3xl text-center">
            <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-accent">The loop</p>
            <h2 className="mt-3 text-balance text-3xl font-semibold leading-[1.08] tracking-[-0.02em] md:text-5xl">
              Five steps.
              <br className="hidden md:block" />{" "}
              The last one is a live URL.
            </h2>
            <p className="mx-auto mt-5 max-w-xl text-[15px] leading-[1.7] text-ink-1">
              Other coding agents stop at code. {BRAND.name} keeps going until
              the app actually opens — in a real browser, with the agent
              watching, fixing what it sees, and only then handing it to you.
            </p>
          </div>

          <div className="mt-16 grid gap-px overflow-hidden rounded-2xl border border-border/60 bg-border/50 sm:grid-cols-2 md:grid-cols-5">
            {STAGES.map((s, i) => {
              const isShip = s.label === "Live";
              return (
                <div
                  key={s.label}
                  className={[
                    "relative flex flex-col gap-2 bg-surface-1 p-6 transition-colors hover:bg-surface-2",
                    isShip ? "ring-1 ring-inset ring-deploy/30" : "",
                  ].join(" ")}
                >
                  <div className="flex items-center gap-2">
                    <span
                      className={[
                        "inline-flex h-6 w-6 items-center justify-center rounded-md font-mono text-[11px]",
                        isShip ? "bg-deploy/15 text-deploy" : "bg-accent/15 text-accent",
                      ].join(" ")}
                    >
                      {i + 1}
                    </span>
                    <h3
                      className={[
                        "text-[15px] font-semibold tracking-tight",
                        isShip ? "text-deploy" : "text-foreground",
                      ].join(" ")}
                    >
                      {s.label}
                    </h3>
                  </div>
                  <p className="text-[13px] leading-[1.55] text-muted-foreground">{s.caption}</p>
                  {isShip && (
                    <div className="mt-3 inline-flex items-center gap-1.5 rounded-md border border-deploy/30 bg-deploy/[0.06] px-2 py-1 font-mono text-[10.5px] text-deploy animate-deploy-pulse">
                      <span className="size-1.5 rounded-full bg-deploy" />
                      preview.deft.build/abcdef
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </section>

      {/* REPLACES — single, confident statement, not a logo wall. */}
      <section className="relative border-t border-border/60">
        <div className="container-deft py-24 md:py-32">
          <div className="grid gap-12 lg:grid-cols-[1.1fr_1fr] lg:gap-20">
            <div>
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-accent">No debug hell</p>
              <h2 className="mt-3 text-balance text-3xl font-semibold leading-[1.1] tracking-[-0.02em] md:text-[44px]">
                The mistakes don{"\u2019"}t become
                <br className="hidden md:block" /> your mistakes.
              </h2>
              <div className="mt-7 space-y-5 text-[15.5px] leading-[1.75] text-ink-1">
                <p>
                  Every other AI coding tool generates code into a void and trusts you
                  to tell it what went wrong. You become the eyes. You read the console.
                  You paste the screenshot. You explain the bug to the AI. You do this
                  for two hours. {BRAND.name} doesn{"\u2019"}t make you do that.
                </p>
                <p>
                  {BRAND.name} writes the code, then runs the app in a real browser. It
                  watches the page render. It reads the console. It catches the error
                  before you do. The version you see is the version that already runs.
                </p>
              </div>
              <div className="mt-9 flex flex-wrap items-center gap-4">
                <Link
                  to="/signup"
                  className="inline-flex items-center gap-1.5 rounded-md bg-accent px-5 py-3 text-[14px] font-medium text-accent-foreground shadow-[0_4px_18px_-6px_hsl(var(--accent)/0.55)] transition-all hover:brightness-110"
                >
                  Try it free
                </Link>
                <Link
                  to="/pricing"
                  className="text-[14px] font-medium text-muted-foreground transition-colors hover:text-foreground"
                >
                  See pricing →
                </Link>
              </div>
            </div>

            {/* Right: a stylized "task receipt" — the artifact users actually get. */}
            <div className="relative">
              <div className="rounded-xl border border-border/70 bg-surface-1 shadow-elev-2">
                <div className="flex items-center gap-2 border-b border-border/70 px-4 py-2.5">
                  <span className="size-2 rounded-full bg-rose-500/70" />
                  <span className="size-2 rounded-full bg-amber-400/70" />
                  <span className="size-2 rounded-full bg-emerald-400/70" />
                  <span className="ml-2 font-mono text-[10.5px] uppercase tracking-[0.16em] text-muted-foreground">
                    deft / receipt
                  </span>
                </div>
                <div className="space-y-1.5 p-5 font-mono text-[12px] leading-6 text-ink-1">
                  <p><span className="text-muted-foreground">▸ goal</span>  habit tracker w/ streak heatmap</p>
                  <p><span className="text-muted-foreground">▸ stack</span> React 19 · Vite · Tailwind · Supabase</p>
                  <p><span className="text-muted-foreground">▸ files</span> 23 · <span className="text-foreground">2,148 LOC</span></p>
                  <p><span className="text-muted-foreground">▸ build</span> green in 14.2s</p>
                  <p><span className="text-muted-foreground">▸ tests</span> 18/18 passed</p>
                  <p><span className="text-muted-foreground">▸ spend</span> 412 credits ($4.12)</p>
                  <div className="mt-3 border-t border-border/70 pt-3">
                    <p className="flex items-center gap-2">
                      <span className="size-1.5 rounded-full bg-deploy animate-pulse" />
                      <span className="text-deploy">live</span>{" "}
                      <span className="text-foreground">preview.deft.build/h4b1ts</span>
                    </p>
                  </div>
                </div>
              </div>
              <div
                className="pointer-events-none absolute -inset-6 -z-10 rounded-[24px] opacity-50 blur-2xl"
                style={{ background: "radial-gradient(closest-side, hsl(var(--accent)/0.25), transparent)" }}
                aria-hidden
              />
            </div>
          </div>
        </div>
      </section>

      {/* PRICING TEASER — outcome-framed (Phase 01 voice rule) */}
      <section className="relative border-t border-border/60 bg-surface-1/40">
        <div className="container-deft py-24 md:py-28">
          <div className="mx-auto max-w-3xl text-center">
            <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-deploy">Pricing</p>
            <h2 className="mt-3 text-balance text-3xl font-semibold leading-[1.1] tracking-[-0.02em] md:text-5xl">
              You only pay for
              <br className="hidden md:block" />{" "}
              <span className="text-deploy">software that runs.</span>
            </h2>
            <p className="mx-auto mt-5 max-w-xl text-[15px] leading-[1.7] text-ink-1">
              Planning, writing, and verifying are free. Credits charge only against
              successful work. If a step fails and {BRAND.name} can{"\u2019"}t recover, the
              credits for that step are not deducted.
            </p>
            <div className="mt-9 flex flex-wrap items-center justify-center gap-4">
              <Link
                to="/signup"
                className="inline-flex items-center gap-1.5 rounded-md bg-accent px-5 py-3 text-[14px] font-medium text-accent-foreground shadow-[0_4px_18px_-6px_hsl(var(--accent)/0.55)] transition-all hover:brightness-110"
              >
                Try it free
              </Link>
              <Link
                to="/pricing"
                className="text-[14px] font-medium text-muted-foreground transition-colors hover:text-foreground"
              >
                Compare plans →
              </Link>
            </div>
          </div>
        </div>
      </section>

      <Footer />
    </div>
  );
}
