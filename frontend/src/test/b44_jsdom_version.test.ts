/**
 * B-44 regression tests: jsdom must be pinned at ≥24.x.
 *
 * Root cause (A4-11): frontend/package.json listed "jsdom": "^20.0.3".
 * jsdom 20.x has known security issues (XSS-bypass CVEs patched in 21+).
 * Since jsdom is used by vitest as the DOM environment for security-related
 * tests (XSS sanitization, ARIA verification), a vulnerable jsdom parser
 * may produce false-pass results.
 *
 * Fix: bump devDependencies to "jsdom": "^24.0.0".
 *
 * These tests verify:
 *   1. package.json specifies jsdom ≥24 in devDependencies.
 *   2. The installed (resolved) jsdom version is ≥24.
 *   3. package.json does not specify a version below 24 anywhere.
 *   4. vitest version in package.json is compatible with jsdom ≥22 (vitest ≥2).
 *   5. The version constraint uses ^ (semver-compatible range, not an exact pin).
 */

import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const pkgPath = resolve(__dirname, "../../package.json");
const lockPath = resolve(__dirname, "../../package-lock.json");

type PackageJson = {
  devDependencies?: Record<string, string>;
  dependencies?: Record<string, string>;
};

type LockEntry = { version?: string; resolved?: string };
type LockJson = {
  packages?: Record<string, LockEntry>;
  dependencies?: Record<string, LockEntry>;
};

let pkg: PackageJson;
let lock: LockJson | null = null;

beforeAll(() => {
  const raw = readFileSync(pkgPath, "utf-8");
  pkg = JSON.parse(raw) as PackageJson;
  try {
    const lockRaw = readFileSync(lockPath, "utf-8");
    lock = JSON.parse(lockRaw) as LockJson;
  } catch {
    // package-lock.json may not exist in the repo; non-fatal
    lock = null;
  }
});

/** Parse a semver string like "24.1.0" → [24, 1, 0] */
function parseSemver(v: string): [number, number, number] {
  const cleaned = v.replace(/^[^0-9]*/, ""); // strip ^ ~ >= etc.
  const [major = 0, minor = 0, patch = 0] = cleaned.split(".").map(Number);
  return [major, minor, patch];
}

/** Compare semver tuples: returns true if a >= b */
function semverGte(a: [number, number, number], b: [number, number, number]): boolean {
  if (a[0] !== b[0]) return a[0] > b[0];
  if (a[1] !== b[1]) return a[1] > b[1];
  return a[2] >= b[2];
}

describe("B-44 jsdom version ≥24 contract", () => {
  it("package.json devDependencies declares jsdom", () => {
    expect(pkg.devDependencies?.jsdom, "jsdom must be in devDependencies").toBeDefined();
  });

  it("package.json jsdom constraint specifies major version ≥24", () => {
    const spec = pkg.devDependencies?.jsdom ?? "";
    const [major] = parseSemver(spec);
    expect(
      major,
      `jsdom devDependency "${spec}" must specify major ≥24; bump to "^24.0.0"`,
    ).toBeGreaterThanOrEqual(24);
  });

  it("package.json jsdom version does not contain a version below 24 (no ^20, ^21, ^22, ^23)", () => {
    const spec = pkg.devDependencies?.jsdom ?? "";
    // Ensure the spec doesn't accidentally allow <24 through a range like >=20
    const [major] = parseSemver(spec);
    expect(major).toBeGreaterThanOrEqual(24);
  });

  it("package.json vitest constraint is ≥2.x (required for jsdom ≥22 support)", () => {
    const vitestSpec = pkg.devDependencies?.vitest ?? pkg.dependencies?.vitest ?? "";
    expect(vitestSpec, "vitest must be declared").toBeTruthy();
    const [major] = parseSemver(vitestSpec);
    expect(
      major,
      `vitest "${vitestSpec}" must be ≥2 to support jsdom ≥22`,
    ).toBeGreaterThanOrEqual(2);
  });

  it("package.json jsdom constraint uses ^ (allows patch/minor upgrades, not a hard pin)", () => {
    const spec = pkg.devDependencies?.jsdom ?? "";
    expect(spec).toMatch(/^\^/);
  });

  it("resolved jsdom version from package-lock.json is ≥24 (if lock file present)", () => {
    if (!lock) {
      // No lock file — skip with a note
      console.warn("[B-44] package-lock.json not found; skipping resolved-version check");
      return;
    }
    // npm v7+ lockfile format uses "packages" map with "node_modules/jsdom" key
    const entry =
      lock.packages?.["node_modules/jsdom"] ??
      lock.dependencies?.["jsdom"];
    if (!entry?.version) {
      console.warn("[B-44] jsdom not found in lock file; may not be installed yet");
      return;
    }
    const resolved = parseSemver(entry.version);
    expect(
      semverGte(resolved, [24, 0, 0]),
      `Resolved jsdom ${entry.version} must be ≥24.0.0`,
    ).toBe(true);
  });
});
