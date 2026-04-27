import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  ReactNode,
} from "react";
import { toast } from "sonner";
import { useQueryClient } from "@tanstack/react-query";
import { supabase } from "@/lib/supabase";
import { identifyUser, resetAnalytics } from "@/lib/analytics";
import { addBreadcrumb, setUserContext } from "@/lib/observability";
import type { Session } from "@supabase/supabase-js";

/** Core user shape stored in context */
interface User {
  id: string;
  email: string;
  name: string;
  tokens: number;
  role: string;                 // "user" | "admin"
  subscription_plan: string;    // "none" | "researcher" | "professional" | "enterprise"
  subscription_status: string;  // "none" | "active" | "canceled" | etc.
}

/** Shape of a row from the public.profiles table */
interface ProfileRow {
  id: string;
  email: string;
  full_name: string | null;
  tokens: number;
  role: string | null;
  subscription_plan: string | null;
  subscription_status: string | null;
}

/** Public API surface of the auth context */
interface AuthContextType {
  user: User | null;
  /** True while the initial session is being resolved. Once false, `user` is stable. */
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, name: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
  /** No-op stub kept for backward compatibility — guest access removed */
  skip: () => void;
  /** Re-fetch the user profile from Supabase to pick up server-side balance changes */
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

/**
 * Fetch the user's profile row from public.profiles.
 * Returns null if the row doesn't exist yet (e.g. trigger hasn't fired).
 */
async function fetchProfile(userId: string): Promise<ProfileRow | null> {
  const { data, error } = await supabase
    .from("profiles")
    .select("id, email, full_name, tokens, role, subscription_plan, subscription_status")
    .eq("id", userId)
    .single();

  if (error) {
    // PGRST116 = no rows found — not a fatal error, just means profile pending
    if (error.code !== "PGRST116") {
      console.error("[AuthContext] fetchProfile error:", error.message);
    }
    return null;
  }
  return data as ProfileRow;
}

/** Convert a Supabase session + profile row into the local User shape */
function buildUser(session: Session, profile: ProfileRow | null): User {
  const email = session.user.email ?? "";
  const metaName =
    (session.user.user_metadata?.full_name as string | undefined) ?? "";
  return {
    id: session.user.id,
    email,
    name: profile?.full_name ?? (metaName || email.split("@")[0]),
    tokens: profile?.tokens ?? 0,
    role: profile?.role ?? "user",
    subscription_plan: profile?.subscription_plan ?? "none",
    subscription_status: profile?.subscription_status ?? "none",
  };
}

/** B-28: Maximum ms to wait for Supabase onAuthStateChange before giving up.
 *  Configurable via VITE_AUTH_TIMEOUT_MS env var (default 10000).
 *  On outage / slow network the app would show an infinite spinner without this. */
export const AUTH_LOADING_TIMEOUT_MS: number =
  typeof import.meta !== "undefined" && (import.meta as Record<string, unknown>).env
    ? Number((import.meta as Record<string, Record<string, string>>).env.VITE_AUTH_TIMEOUT_MS ?? 10000)
    : 10000;

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  // B-28: Track whether the auth timeout fired so we can show a retry UI.
  const [authTimedOut, setAuthTimedOut] = useState(false);
  // BUG-FE-138 fix: Access the shared react-query client so we can clear cached
  // queries on logout. Otherwise the next user to sign in on the same tab could
  // briefly see the previous user's cached data.
  const queryClient = useQueryClient();

  /**
   * Given a session (or null), load the profile and update state.
   * BUG-023: Wrapped in try/catch so network errors don't become unhandled
   * promise rejections in the onAuthStateChange listener.
   */
  const syncSession = useCallback(async (session: Session | null) => {
    if (!session) {
      setUser(null);
      return;
    }
    try {
      // Retry up to 5 times with 500ms delays (2.5 s total) — profile trigger
      // may not have fired immediately after signup (BUG-R2C-11).
      // BUG-FE-131 fix: Extended from 3 to 5 attempts to cover cold/free-tier
      // Supabase instances where the trigger occasionally exceeds 1.5s.
      let profile: ProfileRow | null = null;
      for (let attempt = 0; attempt < 5; attempt++) {
        profile = await fetchProfile(session.user.id);
        if (profile) break;
        await new Promise((r) => setTimeout(r, 500));
      }
      const next = buildUser(session, profile);
      setUser(next);
      // Tie analytics + observability to the signed-in user. Both calls are
      // no-ops when their respective providers are not configured.
      identifyUser(next.id, {
        email: next.email,
        plan: next.subscription_plan,
        status: next.subscription_status,
        role: next.role,
      });
      setUserContext({ id: next.id, email: next.email });
      addBreadcrumb({
        category: "auth",
        message: "session synced",
        data: { plan: next.subscription_plan, status: next.subscription_status },
      });
    } catch (err) {
      console.error("[AuthContext] syncSession error:", err);
      // Still set user with just session data so the app remains usable
      setUser(buildUser(session, null));
    }
  }, []);

  /**
   * BUG-008: Remove explicit getSession() call.
   * Supabase fires onAuthStateChange with INITIAL_SESSION on subscription,
   * so calling getSession() separately causes a double fetchProfile on mount.
   * Rely solely on onAuthStateChange for both initial and subsequent events.
   */
  useEffect(() => {
    let mounted = true;

    // B-28: Timeout guard — if Supabase onAuthStateChange never fires (service
    // outage, network issue, misconfigured env), stop the infinite spinner after
    // AUTH_LOADING_TIMEOUT_MS and surface a recoverable error state.
    const timeoutId = setTimeout(() => {
      if (mounted && loading) {
        console.warn("[AuthContext] Auth initialization timed out — treating as unauthenticated.");
        setLoading(false);
        setAuthTimedOut(true);
        setUser(null);
      }
    }, AUTH_LOADING_TIMEOUT_MS);

    // Listen for all auth events including INITIAL_SESSION
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      if (!mounted) return;
      // Auth responded — cancel the timeout so we don't fire it after a late response.
      clearTimeout(timeoutId);
      syncSession(session).finally(() => {
        if (mounted) {
          setLoading(false);
          setAuthTimedOut(false);
        }
      });
    });

    return () => {
      mounted = false;
      clearTimeout(timeoutId);
      subscription.unsubscribe();
    };
  }, [syncSession]); // eslint-disable-line react-hooks/exhaustive-deps -- loading intentionally excluded: we only want to set up the timer once on mount

  /**
   * Sign in with email + password.
   * Throws on failure so the caller (Login.tsx) can catch and display errors.
   *
   * BUG-R2-07: Do NOT call fetchProfile or setUser here.
   * supabase.auth.signInWithPassword() triggers the SIGNED_IN event on the
   * onAuthStateChange listener, which calls syncSession() — and therefore
   * fetchProfile — automatically. Calling it here too results in two
   * concurrent DB reads with a non-deterministic setUser race.
   */
  const login = async (email: string, password: string): Promise<void> => {
    const { error } = await supabase.auth.signInWithPassword({
      email,
      password,
    });
    if (error) {
      toast.error("Sign in failed", { description: error.message });
      throw error;
    }
    // onAuthStateChange handles SIGNED_IN — no manual fetchProfile/setUser needed
  };

  /**
   * Create a new account with name, email, and password.
   * The Supabase trigger auto-creates the profiles row.
   */
  const signup = async (
    email: string,
    name: string,
    password: string
  ): Promise<boolean> => {
    const { data, error } = await supabase.auth.signUp({
      email,
      password,
      options: {
        data: { full_name: name },
      },
    });
    if (error) {
      toast.error("Sign up failed", { description: error.message });
      throw error;
    }
    // If email confirmation is disabled, a session is returned immediately.
    // Let onAuthStateChange / syncSession handle profile loading — no manual
    // setUser call here to avoid the race condition (BUG-R2C-11).
    if (data.session) {
      return true;
    }
    // Email confirmation required — notify the user and stay on the signup page.
    toast.success("Check your email", {
      description: "We've sent you a confirmation link to complete signup.",
    });
    return false;
  };

  // BUG-R2-12: Destructure the signOut result to log errors.
  // A failed signOut (network error, Supabase outage) was silently swallowed,
  // leaving the server session alive while the client believed it was logged out.
  // We still clear local user state regardless — don't leave the user stuck.
  const logout = async (): Promise<void> => {
    const { error } = await supabase.auth.signOut();
    if (error) {
      console.error("[AuthContext] signOut error:", error.message);
      // Clear local state anyway — better to be logged out locally than stuck
    }
    setUser(null);
    setUserContext(null);
    resetAnalytics();
    addBreadcrumb({ category: "auth", message: "signed out" });
    // BUG-FE-138 fix: Clear react-query cache so the next user on this tab does
    // not briefly observe the previous user's cached responses.
    try {
      queryClient.clear();
    } catch (err) {
      console.warn("[AuthContext] queryClient.clear() failed:", err);
    }
    // FE-HIGH-02 fix: Dispatch a custom event so module-scoped caches (outside
    // React) can clear themselves when the user logs out. This prevents the next
    // user on the same tab from seeing cached data from the previous session.
    window.dispatchEvent(new Event("deft:logout"));
  };

  /**
   * Stub: guest/skip functionality removed.
   * Kept in the context shape so no existing consumers need updating.
   */
  const skip = (): void => {
    console.warn("[AuthContext] skip() is no longer supported.");
  };

  /**
   * BUG-018: Re-fetch the user profile from Supabase.
   * Call after an investigation completes to pick up server-side token balance changes.
   */
  const refreshUser = useCallback(async (): Promise<void> => {
    const { data: { session } } = await supabase.auth.getSession();
    if (session) {
      await syncSession(session);
    }
  }, [syncSession]);

  // BUG-015: Show a loading spinner instead of a blank screen while session loads.
  // B-28: Also show a timeout error if Supabase never responded.
  if (loading) {
    return (
      <div
        role="status"
        aria-live="polite"
        aria-label="Authenticating"
        data-testid="auth-loading"
        className="flex h-screen items-center justify-center bg-background"
      >
        <div aria-hidden className="h-5 w-5 animate-spin rounded-full border-2 border-border border-t-primary" />
        <span className="sr-only">Authenticating</span>
      </div>
    );
  }

  // B-28: Auth timed out — show a recoverable error screen instead of an infinite spinner.
  if (authTimedOut) {
    return (
      <div
        role="alert"
        data-testid="auth-timeout"
        className="flex h-screen flex-col items-center justify-center gap-4 bg-background p-8 text-center"
      >
        <p className="text-sm text-muted-foreground">
          Authentication is taking longer than expected. Check your connection and try again.
        </p>
        <button
          onClick={() => window.location.reload()}
          className="rounded-md border border-border px-4 py-2 text-sm text-foreground hover:bg-accent"
        >
          Retry
        </button>
      </div>
    );
  }

  return (
    <AuthContext.Provider
      value={{ user, loading, login, signup, logout, skip, refreshUser }}
    >
      {children}
    </AuthContext.Provider>
  );
}

/** Must be used within an AuthProvider */
export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
