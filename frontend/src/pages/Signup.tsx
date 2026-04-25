import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { track } from "@/lib/analytics";
import { BrandMark } from "@/components/BrandMark";
import { BRAND } from "@/lib/brand";
import { ArrowRight, CheckCircle2, Sparkles } from "lucide-react";

export default function Signup() {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [pendingNav, setPendingNav] = useState(false);
  const { signup, user } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();

  // `?next=` set by Index.tsx when an unauth visitor submits the landing
  // prompt, or by ProtectedRoute when they hit a gated page directly.  We
  // forward them there post-signup so the prompt round-trip is lossless.
  const nextParam = params.get("next");
  const safeNext = useMemo(() => {
    if (!nextParam) return "/build";
    if (!nextParam.startsWith("/") || nextParam.startsWith("//")) return "/build";
    return nextParam;
  }, [nextParam]);

  useEffect(() => {
    if (pendingNav && user) navigate(safeNext, { replace: true });
  }, [pendingNav, user, navigate, safeNext]);

  // Already-authed visitor on /signup → forward immediately.
  useEffect(() => {
    if (user) navigate(safeNext, { replace: true });
  }, [user, navigate, safeNext]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedEmail = email.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmedEmail)) {
      toast.error("Invalid email", { description: "Please enter a valid email address." });
      return;
    }
    if (password.length < 8) {
      toast.error("Password too short", { description: "Password must be at least 8 characters." });
      return;
    }

    setIsLoading(true);
    try {
      const confirmed = await signup(trimmedEmail, name, password);
      if (confirmed) {
        try {
          track("signup_completed", { method: "password" });
        } catch {
          /* ignore */
        }
        setPendingNav(true);
      }
    } catch {
      /* error already toasted */
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="relative grid min-h-screen grid-cols-1 overflow-hidden bg-background text-foreground lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)]">
      <div className="relative z-10 flex flex-col px-6 py-10 sm:px-10 lg:px-16">
        <div className="mb-12">
          <BrandMark />
        </div>

        <div className="flex flex-1 items-center">
          <div className="w-full max-w-sm">
            <div className="mb-3 inline-flex items-center gap-2 rounded-full border border-border/70 bg-surface-1/70 px-3 py-1 text-[12px] font-medium tracking-[0.01em] text-muted-foreground">
              <Sparkles size={11} className="text-deploy" />
              Free credits to start
            </div>
            <h1 className="text-[32px] font-semibold leading-[1.1] tracking-[-0.02em] text-foreground">
              Software that runs.
              <br />
              From a single prompt.
            </h1>
            <p className="mt-2.5 text-[14.5px] leading-[1.55] text-muted-foreground">
              Create your account and start your first run.
              You only pay when {BRAND.name} delivers software that runs.
            </p>

            <form onSubmit={handleSubmit} className="mt-9 space-y-4">
              <div>
                <label htmlFor="name" className="mb-1.5 block text-[12px] font-medium tracking-[0.01em] text-muted-foreground">
                  Name
                </label>
                <Input
                  id="name"
                  type="text"
                  autoComplete="name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                  placeholder="Your name"
                  disabled={isLoading}
                  className="h-11"
                />
              </div>
              <div>
                <label htmlFor="email" className="mb-1.5 block text-[12px] font-medium tracking-[0.01em] text-muted-foreground">
                  Email
                </label>
                <Input
                  id="email"
                  type="email"
                  autoComplete="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  placeholder="you@example.com"
                  disabled={isLoading}
                  className="h-11"
                />
              </div>
              <div>
                <label htmlFor="password" className="mb-1.5 block text-[12px] font-medium tracking-[0.01em] text-muted-foreground">
                  Password
                </label>
                <Input
                  id="password"
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  minLength={8}
                  placeholder="At least 8 characters"
                  disabled={isLoading}
                  className="h-11"
                />
              </div>

              <Button
                type="submit"
                className="group h-11 w-full gap-2 text-[14px] font-medium"
                disabled={isLoading}
              >
                {isLoading ? "Creating account…" : (
                  <>
                    Create account <ArrowRight size={14} className="transition-transform group-hover:translate-x-0.5" />
                  </>
                )}
              </Button>
            </form>

            <p className="mt-8 text-center text-[12.5px] text-muted-foreground">
              Already on {BRAND.name}?{" "}
              <Link
                to={`/login${nextParam ? `?next=${encodeURIComponent(nextParam)}` : ""}`}
                className="font-medium text-foreground underline-offset-4 hover:underline"
              >
                Sign in
              </Link>
            </p>
          </div>
        </div>

        <p className="mt-12 text-[11px] text-muted-foreground/70">
          By creating an account you agree to our{" "}
          <Link to="/legal/terms" className="hover:text-foreground">Terms</Link> and{" "}
          <Link to="/legal/privacy" className="hover:text-foreground">Privacy</Link>.
        </p>
      </div>

      {/* RIGHT: value-prop visual */}
      <div className="relative hidden overflow-hidden border-l border-border/60 lg:block">
        <div className="absolute inset-0 bg-grid opacity-50" aria-hidden />
        <div className="absolute inset-0 bg-vignette" aria-hidden />
        <div
          className="absolute left-1/2 top-1/3 h-[600px] w-[800px] -translate-x-1/2 -translate-y-1/2 rounded-full opacity-30 blur-3xl"
          style={{ background: "radial-gradient(closest-side, hsl(var(--accent)/0.6), transparent)" }}
          aria-hidden
        />

        <div className="relative flex h-full items-center justify-center px-12">
          <div className="w-full max-w-md space-y-6">
            <p className="text-[12px] font-medium tracking-[0.02em] text-accent">The loop</p>
            <h2 className="text-balance text-[28px] font-semibold leading-[1.15] tracking-[-0.02em]">
              Five steps. The last one is a live URL.
            </h2>

            <ol className="space-y-2.5">
              {[
                { l: "Plan", c: "Break the goal into ordered steps." },
                { l: "Write", c: "Generate every file in a sandbox." },
                { l: "Compile", c: "Build, lint, type-check." },
                { l: "Verify", c: "Open it in a real browser. Catch its own errors." },
                { l: "Live", c: "Push to a public URL.", deploy: true },
              ].map((s, i) => (
                <li
                  key={s.l}
                  className={[
                    "flex items-center gap-3 rounded-lg border bg-surface-1/70 px-3.5 py-2.5",
                    s.deploy ? "border-deploy/40" : "border-border/60",
                  ].join(" ")}
                >
                  <span
                    className={[
                      "inline-flex h-6 w-6 items-center justify-center rounded-md font-mono text-[11px]",
                      s.deploy ? "bg-deploy/15 text-deploy" : "bg-accent/15 text-accent",
                    ].join(" ")}
                  >
                    {i + 1}
                  </span>
                  <span className="flex-1 text-[13.5px] text-foreground">{s.l}</span>
                  <span className="text-[12.5px] text-muted-foreground">{s.c}</span>
                  {s.deploy && <CheckCircle2 size={14} className="text-deploy" />}
                </li>
              ))}
            </ol>

            <p className="text-[12.5px] text-muted-foreground">
              Planning, writing, and verifying are free. Credits charge
              only against successful work.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
