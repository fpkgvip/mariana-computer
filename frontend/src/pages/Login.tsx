import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { supabase } from "@/lib/supabase";
import { BrandMark } from "@/components/BrandMark";
import { BRAND } from "@/lib/brand";
import { ArrowRight, Globe, ShieldCheck } from "lucide-react";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isResetting, setIsResetting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const { login, user } = useAuth();
  const navigate = useNavigate();

  // BUG-R2C-12: navigate AFTER AuthContext.user lands (avoids ProtectedRoute race).
  // After login, send users to /build (the studio is the home for signed-in users).
  useEffect(() => {
    if (submitted && user) navigate("/build");
  }, [submitted, user, navigate]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    try {
      await login(email.trim(), password);
      setSubmitted(true);
    } catch {
      setSubmitted(false);
    } finally {
      setIsLoading(false);
    }
  };

  const handleForgot = async () => {
    if (!email || isResetting) {
      if (!email) toast.info("Enter your email above, then click Forgot password.");
      return;
    }
    setIsResetting(true);
    try {
      const { error } = await supabase.auth.resetPasswordForEmail(email.trim(), {
        redirectTo: `${window.location.origin}/reset-password`,
      });
      if (error) toast.error("Failed to send reset email", { description: error.message });
      else toast.success("Password reset email sent", { description: "Check your inbox for a reset link." });
    } catch (err) {
      toast.error("Failed to send reset email", { description: String(err) });
    } finally {
      setIsResetting(false);
    }
  };

  return (
    <div className="relative grid min-h-screen grid-cols-1 overflow-hidden bg-background text-foreground lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)]">
      {/* LEFT: Form */}
      <div className="relative z-10 flex flex-col px-6 py-10 sm:px-10 lg:px-16">
        <div className="mb-12">
          <BrandMark />
        </div>

        <div className="flex flex-1 items-center">
          <div className="w-full max-w-sm">
            <h1 className="text-[32px] font-semibold leading-[1.1] tracking-[-0.02em] text-foreground">
              Welcome back.
            </h1>
            <p className="mt-2.5 text-[14.5px] leading-[1.55] text-muted-foreground">
              Pick up where {BRAND.name} left off — your apps, runs, and vault are waiting.
            </p>

            <form onSubmit={handleSubmit} className="mt-9 space-y-4">
              <div>
                <label htmlFor="email" className="mb-1.5 block text-[11px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
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
                <div className="mb-1.5 flex items-center justify-between">
                  <label htmlFor="password" className="block text-[11px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
                    Password
                  </label>
                  <button
                    type="button"
                    onClick={handleForgot}
                    className="text-[11.5px] text-muted-foreground transition-colors hover:text-foreground"
                    disabled={isLoading || isResetting}
                  >
                    {isResetting ? "Sending…" : "Forgot password?"}
                  </button>
                </div>
                <Input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  placeholder="••••••••"
                  disabled={isLoading}
                  className="h-11"
                />
              </div>

              <Button
                type="submit"
                className="group h-11 w-full gap-2 text-[14px] font-medium"
                disabled={isLoading}
              >
                {isLoading ? "Signing in…" : (
                  <>
                    Continue to {BRAND.name} <ArrowRight size={14} className="transition-transform group-hover:translate-x-0.5" />
                  </>
                )}
              </Button>
            </form>

            <p className="mt-8 text-center text-[12.5px] text-muted-foreground">
              New to {BRAND.name}?{" "}
              <Link to="/signup" className="font-medium text-foreground underline-offset-4 hover:underline">
                Create an account
              </Link>
            </p>
          </div>
        </div>

        <p className="mt-12 text-[11px] text-muted-foreground/70">
          By continuing you agree to our{" "}
          <Link to="/legal/terms" className="hover:text-foreground">Terms</Link> and{" "}
          <Link to="/legal/privacy" className="hover:text-foreground">Privacy</Link>.
        </p>
      </div>

      {/* RIGHT: Visual */}
      <div className="relative hidden overflow-hidden border-l border-border/60 lg:block">
        <div className="absolute inset-0 bg-grid opacity-50" aria-hidden />
        <div className="absolute inset-0 bg-vignette" aria-hidden />
        <div
          className="absolute left-1/2 top-1/3 h-[600px] w-[800px] -translate-x-1/2 -translate-y-1/2 rounded-full opacity-30 blur-3xl"
          style={{ background: "radial-gradient(closest-side, hsl(var(--accent)/0.6), transparent)" }}
          aria-hidden
        />

        <div className="relative flex h-full items-center justify-center px-12">
          <div className="w-full max-w-md">
            <div className="rounded-2xl border border-border/60 bg-surface-1/80 shadow-elev-3 backdrop-blur">
              <div className="flex items-center gap-2 border-b border-border/60 px-4 py-2.5">
                <span className="size-2 rounded-full bg-rose-500/60" />
                <span className="size-2 rounded-full bg-amber-400/60" />
                <span className="size-2 rounded-full bg-deploy animate-pulse" />
                <span className="ml-2 font-mono text-[10.5px] uppercase tracking-[0.16em] text-muted-foreground">
                  deft / receipt
                </span>
              </div>
              <div className="space-y-1.5 p-5 font-mono text-[12px] leading-6 text-ink-1">
                <p><span className="text-muted-foreground">▸ goal</span>  AI flashcards from a PDF</p>
                <p><span className="text-muted-foreground">▸ stack</span> React 19 · Vite · Tailwind</p>
                <p><span className="text-muted-foreground">▸ files</span> 17 · <span className="text-foreground">1,602 LOC</span></p>
                <p><span className="text-muted-foreground">▸ build</span> green in 9.8s</p>
                <p><span className="text-muted-foreground">▸ tests</span> 12/12 passed</p>
                <div className="mt-3 border-t border-border/60 pt-3">
                  <p className="flex items-center gap-2">
                    <span className="size-1.5 rounded-full bg-deploy animate-pulse" />
                    <span className="text-deploy">deployed</span>{" "}
                    <span className="text-foreground">preview.deft.computer/fL4sH</span>
                  </p>
                </div>
              </div>
            </div>

            <div className="mt-8 space-y-3 text-[13px] text-muted-foreground">
              <div className="flex items-center gap-2.5">
                <ShieldCheck size={14} className="text-deploy" />
                <span>Your vault stays encrypted on-device.</span>
              </div>
              <div className="flex items-center gap-2.5">
                <Globe size={14} className="text-accent" />
                <span>Every successful run ends with a live URL.</span>
              </div>
              <div className="flex items-center gap-2.5">
                <span className="size-1.5 rounded-full bg-accent" />
                <span>Hard credit ceilings. No surprise bills.</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
