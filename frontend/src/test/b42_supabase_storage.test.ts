/**
 * B-42 regression tests: Supabase JWT must NOT use localStorage.
 *
 * Root cause (A4-09): supabase-js v2 defaults to localStorage for session
 * persistence.  localStorage is readable by any JS on the same origin, meaning
 * any XSS can exfiltrate the 60-day refresh token.
 *
 * Fix: supabase.ts now passes a custom SupportedStorage adapter backed by
 * sessionStorage, scoped to the current tab.  This eliminates cross-tab token
 * persistence and reduces the XSS exfiltration window.
 *
 * Trade-off documented in docs/security/ADR-B42-supabase-storage.md:
 *   - Each browser tab starts with an isolated session.
 *   - Session is lost when the tab is closed.
 *   - autoRefreshToken=true keeps the access token alive within the tab.
 *
 * These tests verify:
 *   1. The supabase.ts source file does NOT reference localStorage directly.
 *   2. The createClient call includes a `storage:` option (sessionStorage adapter).
 *   3. The SESSION_STORAGE adapter uses sessionStorage.getItem/setItem/removeItem.
 *   4. The `auth` block sets autoRefreshToken: true and persistSession: true.
 *   5. The ADR file exists and documents the trade-off.
 *   6. The SupportedStorage import is present (TypeScript type-safety).
 *
 * Source-level structural tests are used because the full Supabase client
 * cannot be initialised in jsdom without valid env vars and a real Supabase
 * project.  The assertions directly verify the security-critical code path.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const supabasePath = resolve(__dirname, "../lib/supabase.ts");
// ADR is at mariana/docs/security/ — resolve from frontend/src/test/ up 4 levels to mariana root
const adrPath = resolve(__dirname, "../../../docs/security/ADR-B42-supabase-storage.md");
const source = readFileSync(supabasePath, "utf-8");

describe("B-42 Supabase JWT storage: localStorage not used", () => {
  it("supabase.ts does not call localStorage.setItem or localStorage.getItem directly", () => {
    // Direct localStorage usage would bypass the storage adapter.
    expect(source).not.toMatch(/localStorage\.setItem/);
    expect(source).not.toMatch(/localStorage\.getItem/);
    expect(source).not.toMatch(/localStorage\.removeItem/);
  });

  it("supabase.ts does not pass the default storage (no createClient without storage option)", () => {
    // The old call was: createClient(url, key) — two args, no options.
    // After the fix there must be a third argument object.
    // Regex: createClient(...) must not end at the second argument.
    const simpleCallPattern = /createClient\(\s*supabaseUrl\s*,\s*supabaseAnonKey\s*\)/;
    expect(source).not.toMatch(simpleCallPattern);
  });

  it("supabase.ts passes a `storage:` key in the auth options object", () => {
    expect(source).toMatch(/storage\s*:\s*SESSION_STORAGE/);
  });

  it("SESSION_STORAGE adapter wraps sessionStorage.getItem", () => {
    expect(source).toMatch(/sessionStorage\.getItem/);
  });

  it("SESSION_STORAGE adapter wraps sessionStorage.setItem", () => {
    expect(source).toMatch(/sessionStorage\.setItem/);
  });

  it("SESSION_STORAGE adapter wraps sessionStorage.removeItem", () => {
    expect(source).toMatch(/sessionStorage\.removeItem/);
  });

  it("createClient auth options set autoRefreshToken: true", () => {
    expect(source).toMatch(/autoRefreshToken\s*:\s*true/);
  });

  it("createClient auth options set persistSession: true", () => {
    expect(source).toMatch(/persistSession\s*:\s*true/);
  });

  it("SupportedStorage type is imported from @supabase/supabase-js", () => {
    expect(source).toMatch(/SupportedStorage/);
    expect(source).toMatch(/@supabase\/supabase-js/);
  });

  it("B-42 fix comment references A4-09 or B-42 in the source", () => {
    expect(source).toMatch(/B-42|A4-09/);
  });

  it("ADR-B42-supabase-storage.md exists documenting the trade-off", () => {
    expect(existsSync(adrPath), `ADR file not found at ${adrPath}`).toBe(true);
  });

  it("ADR documents the sessionStorage trade-off (per-tab session isolation)", () => {
    if (!existsSync(adrPath)) return; // guarded by previous test
    const adr = readFileSync(adrPath, "utf-8");
    expect(adr).toMatch(/sessionStorage/i);
    expect(adr).toMatch(/trade.off|tradeoff/i);
  });
});
