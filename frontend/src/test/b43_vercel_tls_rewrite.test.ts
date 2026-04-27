/**
 * B-43 regression tests: vercel.json backend rewrites must use HTTPS + hostname.
 *
 * Root cause (A4-10): vercel.json rewrites targeted http://77.42.3.206:8080
 * (bare IP, plain HTTP).  All authenticated API calls (Authorization: Bearer JWT)
 * were transmitted unencrypted on the Vercel→backend hop.
 *
 * Fix: rewrites now target https://api.deft.computer — a TLS-terminated DNS
 * hostname.  If the IP assignment ever changes, only the DNS record needs to
 * be updated without touching vercel.json.
 *
 * These tests verify:
 *   1. No rewrite destination starts with http:// (plain HTTP forbidden).
 *   2. No rewrite destination targets a bare IPv4 address.
 *   3. /api/:path* destination starts with https:// and uses a hostname.
 *   4. /preview/:path* destination starts with https:// and uses a hostname.
 *   5. The old bare IP (77.42.3.206) is not referenced anywhere in rewrites.
 *   6. Both rewrites preserve the correct source paths.
 *
 * Extended from vercelHeaders.test.ts (B-10) which already validates headers.
 * This file focuses exclusively on the rewrite destination security contract.
 */

import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const vercelJsonPath = resolve(__dirname, "../../vercel.json");

type Rewrite = { source: string; destination: string };
type VercelConfig = {
  rewrites?: Rewrite[];
  headers?: unknown[];
};

let cfg: VercelConfig;
let rewrites: Rewrite[];

beforeAll(() => {
  const raw = readFileSync(vercelJsonPath, "utf-8");
  cfg = JSON.parse(raw) as VercelConfig;
  expect(cfg.rewrites, "vercel.json must declare a `rewrites` array").toBeDefined();
  rewrites = cfg.rewrites!;
});

// Helper: bare IPv4 pattern (four octets, optionally with port)
const BARE_IPV4 = /\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?\b/;

describe("B-43 vercel.json rewrite TLS contract", () => {
  it("no rewrite destination uses plain http://", () => {
    for (const r of rewrites) {
      // Skip the SPA fallback rewrite — its destination is a relative path
      if (!r.destination.startsWith("http")) continue;
      expect(
        r.destination,
        `Rewrite '${r.source}' must use https://, not http://`,
      ).toMatch(/^https:\/\//);
    }
  });

  it("no rewrite destination targets a bare IPv4 address", () => {
    for (const r of rewrites) {
      expect(
        r.destination,
        `Rewrite '${r.source}' must not target a bare IP address`,
      ).not.toMatch(BARE_IPV4);
    }
  });

  it("the old bare IP 77.42.3.206 is not present in any rewrite destination", () => {
    for (const r of rewrites) {
      expect(r.destination).not.toContain("77.42.3.206");
    }
  });

  it("/api/:path* destination starts with https:// and uses a DNS hostname", () => {
    const apiRewrite = rewrites.find((r) => r.source === "/api/:path*");
    expect(apiRewrite, "/api/:path* rewrite must be defined").toBeDefined();
    expect(apiRewrite!.destination).toMatch(/^https:\/\/[a-zA-Z]/);
    // Must contain a dot (hostname), not just a scheme
    const url = new URL(apiRewrite!.destination.replace(":path*", "test"));
    expect(url.hostname).toMatch(/\./);
  });

  it("/preview/:path* destination starts with https:// and uses a DNS hostname", () => {
    const previewRewrite = rewrites.find((r) => r.source === "/preview/:path*");
    expect(previewRewrite, "/preview/:path* rewrite must be defined").toBeDefined();
    expect(previewRewrite!.destination).toMatch(/^https:\/\/[a-zA-Z]/);
    const url = new URL(previewRewrite!.destination.replace(":path*", "test"));
    expect(url.hostname).toMatch(/\./);
  });

  it("/api/:path* destination path includes /api/:path*", () => {
    const apiRewrite = rewrites.find((r) => r.source === "/api/:path*");
    expect(apiRewrite!.destination).toContain("/api/");
  });

  it("/preview/:path* destination path includes /preview/:path*", () => {
    const previewRewrite = rewrites.find((r) => r.source === "/preview/:path*");
    expect(previewRewrite!.destination).toContain("/preview/");
  });

  it("SPA fallback rewrite still routes /(.*) to /index.html", () => {
    const spaRewrite = rewrites.find((r) => r.source === "/(.*)" );
    expect(spaRewrite, "SPA fallback rewrite must be defined").toBeDefined();
    expect(spaRewrite!.destination).toBe("/index.html");
  });
});
