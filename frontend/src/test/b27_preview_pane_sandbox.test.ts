/**
 * B-27 regression tests: PreviewPane iframe sandbox attribute.
 *
 * The iframe must NOT contain allow-same-origin alongside allow-scripts.
 * Having both in a sandboxed iframe that loads user-generated content on
 * the app's own origin is equivalent to no sandbox — the iframe JS can
 * read parent localStorage, cookies (non-httpOnly), and the DOM.
 *
 * We test both the raw string value and the structural invariant.
 */

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const previewPanePath = resolve(__dirname, "../components/deft/PreviewPane.tsx");
const source = readFileSync(previewPanePath, "utf-8");

// Extract the sandbox attribute value(s) from the source.
// We look for  sandbox="..."  in the JSX.
function extractSandboxValues(src: string): string[] {
  const matches: string[] = [];
  const re = /sandbox=["']([^"']+)["']/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) {
    matches.push(m[1]);
  }
  return matches;
}

describe("B-27 PreviewPane iframe sandbox isolation", () => {
  it("PreviewPane.tsx source file exists and is readable", () => {
    expect(source.length).toBeGreaterThan(0);
  });

  it("iframe has at least one sandbox attribute", () => {
    const values = extractSandboxValues(source);
    expect(values.length).toBeGreaterThan(0);
  });

  it("sandbox does NOT contain allow-same-origin", () => {
    const values = extractSandboxValues(source);
    for (const val of values) {
      expect(val, `sandbox value "${val}" must not contain allow-same-origin`).not.toContain("allow-same-origin");
    }
  });

  it("sandbox still includes allow-scripts (needed for preview apps)", () => {
    const values = extractSandboxValues(source);
    const hasScripts = values.some((v) => v.includes("allow-scripts"));
    expect(hasScripts).toBe(true);
  });

  it("sandbox still includes allow-forms (needed for form-based preview apps)", () => {
    const values = extractSandboxValues(source);
    const hasForms = values.some((v) => v.includes("allow-forms"));
    expect(hasForms).toBe(true);
  });

  it("sandbox does NOT simultaneously allow allow-scripts + allow-same-origin (the dangerous combination)", () => {
    const values = extractSandboxValues(source);
    for (const val of values) {
      const hasScripts = val.includes("allow-scripts");
      const hasSameOrigin = val.includes("allow-same-origin");
      expect(
        hasScripts && hasSameOrigin,
        `Detected dangerous combination: allow-scripts + allow-same-origin in sandbox="${val}"`
      ).toBe(false);
    }
  });

  it("sandbox value is the expected safe string (snapshot)", () => {
    // This pins the exact value so any future unintentional change is caught.
    const expectedSandbox = "allow-scripts allow-forms allow-modals allow-popups allow-downloads";
    const values = extractSandboxValues(source);
    expect(values.some((v) => v === expectedSandbox)).toBe(true);
  });
});
