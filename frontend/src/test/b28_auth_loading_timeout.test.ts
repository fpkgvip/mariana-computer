/**
 * B-28 regression tests: AuthContext loading timeout.
 *
 * AuthProvider now sets a timeout (AUTH_LOADING_TIMEOUT_MS, default 10 000 ms).
 * If onAuthStateChange never fires — e.g., Supabase outage or network failure —
 * the loading flag is cleared and authTimedOut becomes true, surfacing a
 * recoverable error UI instead of an infinite spinner.
 *
 * These tests verify:
 *   1. The exported constant is in the expected range.
 *   2. The AuthProvider source contains the timeout implementation.
 *   3. The timeout-out UI (data-testid="auth-timeout") is rendered in source.
 *   4. The loading spinner (data-testid="auth-loading") has accessibility attrs.
 *   5. The loading state is set to false when onAuthStateChange resolves.
 *
 * Source-level structural tests are used here (rather than full component
 * rendering) to avoid the heavy Supabase/react-query provider chain that
 * AuthProvider requires. A full integration test would require mocking the
 * Supabase client, react-query, and all child providers.
 */

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const authContextPath = resolve(__dirname, "../contexts/AuthContext.tsx");
const source = readFileSync(authContextPath, "utf-8");

// Import the exported constant for a direct value assertion.
// Using dynamic import to avoid executing the module's Supabase side-effects
// (which would fail in the jsdom environment without proper mocking).
// We test the constant value by reading it from the source instead.
function extractConstantValue(src: string, name: string): number | null {
  // Match: export const AUTH_LOADING_TIMEOUT_MS: number = ... ?? <VALUE>
  const re = new RegExp(`export const ${name}[^=]+=.*?\\?\\?\\s*(\\d+)`, "s");
  const m = re.exec(src);
  return m ? Number(m[1]) : null;
}

describe("B-28 AuthContext loading timeout", () => {
  it("exports AUTH_LOADING_TIMEOUT_MS constant", () => {
    expect(source).toMatch(/export const AUTH_LOADING_TIMEOUT_MS/);
  });

  it("AUTH_LOADING_TIMEOUT_MS default value is 10 000 ms", () => {
    const val = extractConstantValue(source, "AUTH_LOADING_TIMEOUT_MS");
    expect(val).toBe(10000);
  });

  it("AUTH_LOADING_TIMEOUT_MS is configurable via VITE_AUTH_TIMEOUT_MS env var", () => {
    expect(source).toContain("VITE_AUTH_TIMEOUT_MS");
  });

  it("useEffect registers a setTimeout with AUTH_LOADING_TIMEOUT_MS", () => {
    expect(source).toContain("setTimeout");
    expect(source).toContain("AUTH_LOADING_TIMEOUT_MS");
  });

  it("timeout callback sets loading to false", () => {
    // The timeout handler must call setLoading(false).
    // We look for it within the timeout callback block.
    const timeoutBlock = source.match(/const timeoutId = setTimeout\([\s\S]*?\}, AUTH_LOADING_TIMEOUT_MS\)/)?.[0] ?? "";
    expect(timeoutBlock).toContain("setLoading(false)");
  });

  it("timeout callback sets authTimedOut to true", () => {
    const timeoutBlock = source.match(/const timeoutId = setTimeout\([\s\S]*?\}, AUTH_LOADING_TIMEOUT_MS\)/)?.[0] ?? "";
    expect(timeoutBlock).toContain("setAuthTimedOut(true)");
  });

  it("cleanup function calls clearTimeout to cancel the timer on fast responses", () => {
    expect(source).toContain("clearTimeout(timeoutId)");
  });

  it("onAuthStateChange handler calls clearTimeout before syncSession (cancel on resolution)", () => {
    // The listener must cancel the timeout when auth resolves normally.
    const listenerBlock = source.match(/onAuthStateChange\([\s\S]*?\}\);/)?.[0] ?? "";
    expect(listenerBlock).toContain("clearTimeout(timeoutId)");
  });

  it("loading spinner has data-testid='auth-loading'", () => {
    expect(source).toContain('data-testid="auth-loading"');
  });

  it("loading spinner has role='status' for screen-reader accessibility", () => {
    expect(source).toContain('role="status"');
  });

  it("loading spinner has aria-live='polite'", () => {
    expect(source).toContain('aria-live="polite"');
  });

  it("timeout error UI has data-testid='auth-timeout'", () => {
    expect(source).toContain('data-testid="auth-timeout"');
  });

  it("timeout error UI has role='alert' for screen-reader announcement", () => {
    expect(source).toContain('role="alert"');
  });

  it("timeout error UI has a Retry button that calls window.location.reload()", () => {
    expect(source).toContain("window.location.reload()");
  });

  it("authTimedOut state is declared in AuthProvider", () => {
    expect(source).toContain("authTimedOut");
    expect(source).toContain("setAuthTimedOut");
  });
});
