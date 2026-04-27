// B-42 fix: Import SupportedStorage type for the sessionStorage adapter.
import { createClient, type SupabaseClient, type SupportedStorage } from "@supabase/supabase-js";

// BUG-028: Avoid unsafe `as string` casts — preserve the true type (string | undefined)
// so TypeScript can enforce the guards below instead of lying to the compiler.
const supabaseUrl: string | undefined = import.meta.env.VITE_SUPABASE_URL;
const supabaseAnonKey: string | undefined = import.meta.env.VITE_SUPABASE_ANON_KEY;

/**
 * BUG-FE-132 fix: Instead of throwing at module import time (which blanks the
 * screen before the ErrorBoundary can render), we detect missing configuration
 * and expose it via `supabaseConfigError`. App.tsx checks this flag on mount
 * and renders a friendly configuration error screen if it is set.
 *
 * The `supabase` export is a lazy Proxy: access to any property will throw a
 * clear error if the client was never initialized. This preserves the shape
 * of a real SupabaseClient for consumers that destructure it at module scope
 * (e.g. `const { from } = supabase`).
 */
export const supabaseConfigError: string | null = (() => {
  if (!supabaseUrl) return "Missing environment variable: VITE_SUPABASE_URL";
  if (!supabaseAnonKey) return "Missing environment variable: VITE_SUPABASE_ANON_KEY";
  return null;
})();

let _client: SupabaseClient | null = null;
function getClient(): SupabaseClient {
  if (_client) return _client;
  if (!supabaseUrl || !supabaseAnonKey) {
    // App.tsx renders a configuration error screen before any consumer runs,
    // so this branch is a defensive fallback (e.g. tests bypassing the boot).
    throw new Error(
      supabaseConfigError ?? "Supabase client is not configured.",
    );
  }
  // B-42 fix: Use sessionStorage instead of the default localStorage so that
  // the Supabase JWT (access_token + refresh_token) is not persisted across
  // browser tabs/sessions.  localStorage is readable by any JS on the same
  // origin; sessionStorage narrows the XSS exfiltration window to the current
  // tab and clears automatically when the tab closes.
  //
  // Trade-off: each new tab requires a fresh sign-in (or Supabase's built-in
  // refresh-token rotation will re-hydrate the session via the tab's own
  // sessionStorage after the first load if the user has a valid refresh token
  // in that tab).  This is a deliberate UX trade-off accepted to reduce the
  // 60-day refresh-token exfiltration surface documented in A4-09.
  //
  // If per-tab session isolation proves too disruptive, the alternative is
  // memory-only storage (see the commented MEMORY_STORAGE below) which loses
  // the session on any navigation but gives the smallest possible attack surface.
  // See ADR in docs/security/ADR-B42-supabase-storage.md for full rationale.
  const SESSION_STORAGE: SupportedStorage = {
    getItem: (key: string) => sessionStorage.getItem(key),
    setItem: (key: string, value: string) => sessionStorage.setItem(key, value),
    removeItem: (key: string) => sessionStorage.removeItem(key),
  };
  _client = createClient(supabaseUrl, supabaseAnonKey, {
    auth: {
      storage: SESSION_STORAGE,
      // Keep auto-refresh enabled so the access token is silently refreshed
      // within the tab lifetime.
      autoRefreshToken: true,
      // Persist session within this tab so page reloads do not log the user out.
      persistSession: true,
    },
  });
  return _client;
}

// Lazy Proxy: defers client construction until first property access. When
// env vars are present, behaves identically to a directly-constructed client.
export const supabase: SupabaseClient = new Proxy({} as SupabaseClient, {
  get(_target, prop, receiver) {
    const client = getClient();
    const value = Reflect.get(client as object, prop, receiver);
    // Bind functions to the real client so `this` stays correct.
    return typeof value === "function" ? value.bind(client) : value;
  },
});
