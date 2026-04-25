import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { supabase } from "@/lib/supabase";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isResetting, setIsResetting] = useState(false); // BUG-FE-104
  const [submitted, setSubmitted] = useState(false);
  const { login, user } = useAuth();
  const navigate = useNavigate();

  // BUG-R2C-12 fix: navigate AFTER the AuthContext user becomes available.
  // Doing navigate("/chat") synchronously after `await login()` races with the
  // onAuthStateChange listener — ProtectedRoute then sees user==null and bounces
  // us back to /login. Watch the user state instead.
  useEffect(() => {
    if (submitted && user) {
      navigate("/chat");
    }
  }, [submitted, user, navigate]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    try {
      // BUG-FE-140: Trim whitespace from email to prevent auth failures on paste
      await login(email.trim(), password);
      setSubmitted(true);
      // Navigation happens via the useEffect once `user` is populated.
    } catch {
      // Error toast already shown by AuthContext.login()
      setSubmitted(false);
    } finally {
      setIsLoading(false);
    }
  };

  // BUG-FE-104 fix: Add loading state, error trapping, and double-click guard
  const handleForgot = async () => {
    if (!email || isResetting) {
      if (!email) toast.info("Enter your email address above, then click Forgot password.");
      return;
    }
    setIsResetting(true);
    try {
      const { error } = await supabase.auth.resetPasswordForEmail(email.trim(), {
        redirectTo: `${window.location.origin}/reset-password`,
      });
      if (error) {
        toast.error("Failed to send reset email", { description: error.message });
      } else {
        toast.success("Password reset email sent", {
          description: "Check your inbox for a reset link.",
        });
      }
    } catch (err) {
      toast.error("Failed to send reset email", { description: String(err) });
    } finally {
      setIsResetting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6">
      <div className="w-full max-w-sm">
        <Link to="/" className="mb-10 block text-lg font-semibold tracking-tight text-foreground">
          Deft
        </Link>

        <h1 className="text-2xl font-semibold tracking-tight text-foreground">Sign in</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Pick up where Deft left off.
        </p>

        <form onSubmit={handleSubmit} className="mt-8 space-y-4">
          <div>
            <label htmlFor="email" className="mb-1.5 block text-xs font-medium text-muted-foreground">Email</label>
            <Input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              placeholder="you@firm.com"
              disabled={isLoading}
            />
          </div>
          <div>
            <label htmlFor="password" className="mb-1.5 block text-xs font-medium text-muted-foreground">Password</label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              placeholder="••••••••"
              disabled={isLoading}
            />
          </div>

          <div className="flex justify-end">
            <button
              type="button"
              onClick={handleForgot}
              className="text-xs text-muted-foreground hover:text-foreground"
              disabled={isLoading || isResetting}
            >
              Forgot password?
            </button>
          </div>

          <Button type="submit" className="w-full" disabled={isLoading}>
            {isLoading ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <p className="mt-8 text-center text-xs text-muted-foreground">
          Don't have an account?{" "}
          <Link to="/signup" className="font-medium text-foreground hover:underline">Sign up</Link>
        </p>
      </div>
    </div>
  );
}
