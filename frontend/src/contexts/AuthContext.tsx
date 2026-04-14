import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  ReactNode,
} from "react";
import { toast } from "sonner";
import { supabase } from "@/lib/supabase";
import type { Session } from "@supabase/supabase-js";

/** Core user shape stored in context */
interface User {
  id: string;
  email: string;
  name: string;
  tokens: number;
}

/** Shape of a row from the public.profiles table */
interface ProfileRow {
  id: string;
  email: string;
  full_name: string | null;
  tokens: number;
}

/** Public API surface of the auth context */
interface AuthContextType {
  user: User | null;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, name: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** No-op stub kept for backward compatibility — guest access removed */
  skip: () => void;
  addTokens: (amount: number) => void;
  /** Purchase credits — adds tokens equivalent to dollar amount × 10 */
  buyCredits: (dollars: number) => void;
}

const AuthContext = createContext<AuthContextType | null>(null);

/**
 * Fetch the user's profile row from public.profiles.
 * Returns null if the row doesn't exist yet (e.g. trigger hasn't fired).
 */
async function fetchProfile(userId: string): Promise<ProfileRow | null> {
  const { data, error } = await supabase
    .from("profiles")
    .select("id, email, full_name, tokens")
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
    tokens: profile?.tokens ?? 500,
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  /**
   * Given a session (or null), load the profile and update state.
   * Called both on initial mount and on every auth state change.
   */
  const syncSession = useCallback(async (session: Session | null) => {
    if (!session) {
      setUser(null);
      return;
    }
    const profile = await fetchProfile(session.user.id);
    setUser(buildUser(session, profile));
  }, []);

  /** Bootstrap: get the current session from Supabase storage */
  useEffect(() => {
    let mounted = true;

    supabase.auth.getSession().then(({ data: { session } }) => {
      if (!mounted) return;
      syncSession(session).finally(() => {
        if (mounted) setLoading(false);
      });
    });

    // Listen for future auth events (login, logout, token refresh, etc.)
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      syncSession(session);
    });

    return () => {
      mounted = false;
      subscription.unsubscribe();
    };
  }, [syncSession]);

  /**
   * Sign in with email + password.
   * Throws on failure so the caller (Login.tsx) can catch and display errors.
   */
  const login = async (email: string, password: string): Promise<void> => {
    const { data, error } = await supabase.auth.signInWithPassword({
      email,
      password,
    });
    if (error) {
      toast.error("Sign in failed", { description: error.message });
      throw error;
    }
    if (data.session) {
      const profile = await fetchProfile(data.session.user.id);
      setUser(buildUser(data.session, profile));
    }
  };

  /**
   * Create a new account with name, email, and password.
   * The Supabase trigger auto-creates the profiles row.
   */
  const signup = async (
    email: string,
    name: string,
    password: string
  ): Promise<void> => {
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
    // If email confirmation is disabled, session is available immediately
    if (data.session) {
      // Profile trigger may not have fired yet — retry up to 3 times
      let profile: ProfileRow | null = null;
      for (let attempt = 0; attempt < 3; attempt++) {
        profile = await fetchProfile(data.session.user.id);
        if (profile) break;
        await new Promise((r) => setTimeout(r, 500));
      }
      setUser(buildUser(data.session, profile));
    } else {
      // Email confirmation required — let the page handle the UI
      toast.success("Check your email", {
        description: "We've sent you a confirmation link to complete signup.",
      });
    }
  };

  const logout = async (): Promise<void> => {
    await supabase.auth.signOut();
    setUser(null);
  };

  /**
   * Stub: guest/skip functionality removed.
   * Kept in the context shape so no existing consumers need updating.
   */
  const skip = (): void => {
    console.warn("[AuthContext] skip() is no longer supported.");
  };

  const addTokens = (amount: number): void => {
    if (user) setUser({ ...user, tokens: user.tokens + amount });
  };

  const buyCredits = (dollars: number): void => {
    addTokens(dollars * 10);
  };

  // Don't render children until the session check completes
  if (loading) return null;

  return (
    <AuthContext.Provider
      value={{ user, login, signup, logout, skip, addTokens, buyCredits }}
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
