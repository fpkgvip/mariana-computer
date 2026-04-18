import { createClient, type SupabaseClient } from "@supabase/supabase-js";

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
  _client = createClient(supabaseUrl, supabaseAnonKey);
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
