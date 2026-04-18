import { useState, useEffect, useRef } from "react";
import { Link, useNavigate } from "react-router-dom";
import { supabase } from "@/lib/supabase";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";

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

  // Supabase appends the recovery token as a URL hash fragment.
  // onAuthStateChange fires with event=PASSWORD_RECOVERY when it detects it.
  // BUG-R1-06: Add a 10-second timeout so users with expired/invalid links
  // don’t get stuck on "Verifying your reset link…" forever.
  // BUG-R2-04: Empty deps array — run once on mount only.
  // isReady was previously in deps, causing the subscription to be torn down and recreated
  // every time PASSWORD_RECOVERY fired and set isReady=true, creating a leaked subscription.
  // The timeout callback uses a functional setter so it correctly handles the case where
  // isReady was already set to true before the timeout fires.
  // BUG-R2-S2-05: The previous timeout handler checked `prev` (isError) instead of isReady.
  // If PASSWORD_RECOVERY fires before 10s, clearTimeout prevents it. But if the timeout
  // callback was already queued (race), it set isError=true even though isReady=true,
  // showing both the form AND the error banner simultaneously.
  // Fix: use a ref to track readiness so the timeout callback can check it synchronously.
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
  }, []); // no deps — run once on mount

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
      // P1-FIX-85: Handle network errors from updateUser
      const msg = err instanceof Error ? err.message : "Network error";
      toast.error("Failed to update password", { description: msg });
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6">
      <div className="w-full max-w-sm">
        <Link to="/" className="mb-10 block font-serif text-lg font-semibold text-foreground">
          Mariana
        </Link>

        <h1 className="font-serif text-2xl font-semibold text-foreground">
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
          <div className="mt-6 rounded-md border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">
            <p>Your password reset link has expired or is invalid.</p>
            <p className="mt-2">
              <Link to="/login" className="font-medium underline hover:text-red-300">
                Request a new password reset
              </Link>
            </p>
          </div>
        )}

        {isReady && (
          <form onSubmit={handleSubmit} className="mt-8 space-y-4">
            <div>
              <label htmlFor="new-password" className="mb-1.5 block text-xs font-medium text-muted-foreground">
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
              {/* BUG-R1-16: Show minimum length hint before submission */}
              <p className="mt-1 text-xs text-muted-foreground">Minimum 8 characters</p>
            </div>
            <div>
              <label htmlFor="confirm-password" className="mb-1.5 block text-xs font-medium text-muted-foreground">
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
          <Link to="/login" className="font-medium text-foreground hover:underline">
            Back to sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
