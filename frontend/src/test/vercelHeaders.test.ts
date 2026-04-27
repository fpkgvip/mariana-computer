// B-10 contract: vercel.json must declare a strict set of security headers.
//
// Rationale: Vercel applies these headers at the edge for every response.
// Without them the SPA inherits Vercel defaults (no CSP, no HSTS, no
// X-Frame-Options), which leaves us exposed to clickjacking, mixed-content
// downgrades, and uncontrolled script origins.
//
// This test pins the exact set of headers we require and validates their
// values are non-trivial.  When the policy is intentionally tightened or
// relaxed, update both vercel.json AND this test.

import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const vercelJsonPath = resolve(__dirname, "../../vercel.json");

type HeaderEntry = { key: string; value: string };
type HeadersBlock = { source: string; headers: HeaderEntry[] };
type VercelConfig = { headers?: HeadersBlock[] };

let cfg: VercelConfig;
let rootHeaders: Map<string, string>;

beforeAll(() => {
  const raw = readFileSync(vercelJsonPath, "utf-8");
  cfg = JSON.parse(raw) as VercelConfig;
  expect(cfg.headers, "vercel.json must declare a top-level `headers` array").toBeDefined();
  const block = cfg.headers!.find((h) => h.source === "/(.*)");
  expect(block, "vercel.json must declare a `/(.*)` headers block applying to every route").toBeDefined();
  rootHeaders = new Map(block!.headers.map((h) => [h.key, h.value] as const));
});

describe("B-10 vercel.json security headers", () => {
  it("declares Strict-Transport-Security with long max-age, subdomains, and preload", () => {
    const v = rootHeaders.get("Strict-Transport-Security");
    expect(v, "HSTS header must be present").toBeDefined();
    // At least 1 year (31536000) and ideally 2 years (63072000)
    const m = v!.match(/max-age\s*=\s*(\d+)/);
    expect(m, "HSTS must specify max-age").not.toBeNull();
    expect(Number(m![1])).toBeGreaterThanOrEqual(31536000);
    expect(v).toMatch(/includeSubDomains/i);
    expect(v).toMatch(/preload/i);
  });

  it("declares X-Content-Type-Options: nosniff", () => {
    expect(rootHeaders.get("X-Content-Type-Options")).toBe("nosniff");
  });

  it("declares X-Frame-Options: DENY (we render no first-party iframe ancestors)", () => {
    expect(rootHeaders.get("X-Frame-Options")).toBe("DENY");
  });

  it("declares Referrer-Policy at strict-origin-when-cross-origin or stricter", () => {
    const v = rootHeaders.get("Referrer-Policy");
    expect(v, "Referrer-Policy must be present").toBeDefined();
    expect([
      "no-referrer",
      "same-origin",
      "strict-origin",
      "strict-origin-when-cross-origin",
    ]).toContain(v);
  });

  it("declares a Permissions-Policy that disables camera, microphone, and geolocation by default", () => {
    const v = rootHeaders.get("Permissions-Policy");
    expect(v, "Permissions-Policy must be present").toBeDefined();
    expect(v).toMatch(/camera=\(\)/);
    expect(v).toMatch(/microphone=\(\)/);
    expect(v).toMatch(/geolocation=\(\)/);
  });

  it("declares Cross-Origin-Opener-Policy: same-origin", () => {
    expect(rootHeaders.get("Cross-Origin-Opener-Policy")).toBe("same-origin");
  });

  it("declares Cross-Origin-Resource-Policy", () => {
    const v = rootHeaders.get("Cross-Origin-Resource-Policy");
    expect(v, "CORP must be present").toBeDefined();
    expect(["same-origin", "same-site"]).toContain(v);
  });

  it("declares a Content-Security-Policy that covers the directives we depend on", () => {
    const csp = rootHeaders.get("Content-Security-Policy");
    expect(csp, "CSP header must be present").toBeDefined();

    // Required directives.
    const directives = [
      "default-src",
      "base-uri",
      "frame-ancestors",
      "object-src",
      "form-action",
      "img-src",
      "font-src",
      "style-src",
      "script-src",
      "connect-src",
      "frame-src",
      "worker-src",
    ];
    for (const d of directives) {
      expect(csp, `CSP must declare ${d}`).toMatch(new RegExp(`(^|;)\\s*${d}\\b`));
    }

    // Critical lockdowns.
    expect(csp).toMatch(/object-src\s+'none'/);
    expect(csp).toMatch(/frame-ancestors\s+'none'/);
    expect(csp).toMatch(/base-uri\s+'self'/);

    // No wildcard default-src or script-src.
    const defaultSrc = csp!.match(/default-src\s+([^;]+)/)?.[1] ?? "";
    expect(defaultSrc.trim()).not.toMatch(/(^|\s)\*(\s|$)/);

    const scriptSrc = csp!.match(/script-src\s+([^;]+)/)?.[1] ?? "";
    expect(scriptSrc.trim(), "script-src must not be wildcard").not.toMatch(/(^|\s)\*(\s|$)/);
    // We must NOT allow 'unsafe-inline' in script-src; CSS may need it but JS must not.
    expect(scriptSrc, "script-src must not contain 'unsafe-inline'").not.toMatch(/'unsafe-inline'/);
    expect(scriptSrc, "script-src must not contain 'unsafe-eval'").not.toMatch(/'unsafe-eval'/);

    // We use Supabase realtime over WSS and the REST API over HTTPS.
    const connectSrc = csp!.match(/connect-src\s+([^;]+)/)?.[1] ?? "";
    expect(connectSrc).toMatch(/'self'/);
    expect(connectSrc).toMatch(/https:\/\/\*\.supabase\.co/);
    expect(connectSrc).toMatch(/wss:\/\/\*\.supabase\.co/);

    // Stripe Checkout / Elements live in an iframe from js.stripe.com.
    const frameSrc = csp!.match(/frame-src\s+([^;]+)/)?.[1] ?? "";
    expect(frameSrc).toMatch(/js\.stripe\.com/);
  });

  it("rewrites block is preserved (regression guard for accidental config rewrites)", () => {
    const cfgAny = cfg as VercelConfig & { rewrites?: { source: string; destination: string }[] };
    expect(cfgAny.rewrites, "rewrites must remain in vercel.json").toBeDefined();
    const sources = cfgAny.rewrites!.map((r) => r.source);
    expect(sources).toContain("/api/:path*");
    expect(sources).toContain("/preview/:path*");
    expect(sources).toContain("/(.*)");
  });
});
