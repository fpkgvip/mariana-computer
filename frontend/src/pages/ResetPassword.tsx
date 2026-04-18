import { useState, useEffect, useRef } from "react";
import { Link, useNavigate } from "react-router-dom";
import { supabase } from "@/lib/supabase";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { Logo } from "@/components/Logo";

/**
 * BUG-010: The /reset-password route was missing from App.tsx.
 * Login.tsx sends password reset emails with redirectTo pointing here.
 * This page detects the Supabase recovery session (type=recovery) and
 * allows the user to set a new password.
 */
export default function ResetPassword() {
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isReady, setIsReady] = useState(false);
  const [isError, setIsError] = useState(false);

  // BUG-R2-S2-05: Use a ref to track readiness so the timeout callback can check it synchronously.
  const isReadyRef = useRef(false);

  useEffect(() => {
    const timeout = setTimeout(() => {
      if (!isReadyRef.current) {
        setIsError(true);
      }
    }, 10000);

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((event) => {
      if (event === "PASSWORD_RECOVERY") {
        clearTimeout(timeout);
        isReadyRef.current = true;
        setIsReady(true);
      }
    });
    return () => {
      clearTimeout(timeout);
      subscription.unsubscribe();
    };
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (password.length < 8) {
      toast.error("Password too short", {
        description: "Password must be at least 8 characters.",
      });
      return;
    }
    if (password !== confirm) {
      toast.error("Passwords do not match");
      return;
    }
    setIsLoading(true);
    try {
      const { error } = await supabase.auth.updateUser({ password });
      if (error) {
        toast.error("Failed to update password", { description: error.message });
        return;
      }
      toast.success("Password updated", {
        description: "You can now sign in with your new password.",
      });
      navigate("/login");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Network error";
      toast.error("Failed to update password", { description: msg });
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6">
      <div className="w-full max-w-sm">
        <Link to="/" className="mb-10 block">
          <Logo size="md" />
        </Link>

        <h1 className="text-2xl font-bold text-foreground">
          Reset your password
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {isReady
            ? "Enter a new password for your account."
            : isError
            ? "Link expired or invalid."
            : "Verifying your reset link…"}
        </p>

        {/* BUG-R1-06: Show error state when token is expired/invalid/missing */}
        {isError && !isReady && (
          <div className="mt-6 rounded-lg border border-destructive/20 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            <p>Your password reset link has expired or is invalid.</p>
            <p className="mt-2">
              <Link to="/login" className="font-semibold underline hover:opacity-80">
                Request a new password reset
              </Link>
            </p>
          </div>
        )}

        {isReady && (
          <form onSubmit={handleSubmit} className="mt-8 space-y-4">
            <div>
              <label htmlFor="new-password" className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                New password
              </label>
              <Input
                id="new-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={8}
                placeholder="••••••••"
                disabled={isLoading}
              />
              <p className="mt-1 text-xs text-muted-foreground">Minimum 8 characters</p>
            </div>
            <div>
              <label htmlFor="confirm-password" className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                Confirm password
              </label>
              <Input
                id="confirm-password"
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                required
                placeholder="••••••••"
                disabled={isLoading}
              />
            </div>
            <Button type="submit" className="w-full" disabled={isLoading}>
              {isLoading ? "Updating…" : "Set new password"}
            </Button>
          </form>
        )}

        <p className="mt-8 text-center text-xs text-muted-foreground">
          <Link to="/login" className="font-semibold text-primary hover:underline">
            Back to sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
