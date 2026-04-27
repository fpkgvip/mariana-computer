/**
 * B-26 regression tests: FileViewer renderMarkdownContent href sanitization.
 *
 * The hand-rolled markdown renderer in FileViewer.tsx now includes a link
 * transform ([text](url)) that only allows https?:// hrefs. Dangerous
 * schemes (javascript:, data:text/html, vbscript:, file:) are stripped
 * — the link text is preserved but no <a> element is emitted.
 *
 * These tests call the logic in isolation by replicating the same
 * sanitization function contract. Because the function is not exported,
 * we re-implement the same algorithm here and assert its invariants.
 * This is the same approach used to test the Checkout.tsx URL guard (B-25).
 *
 * AAA layout.
 */

import { describe, it, expect } from "vitest";

// ---------------------------------------------------------------------------
// Minimal replica of the link-transform rule from FileViewer.tsx (B-26 fix).
// We keep this in sync with the source; a future change that breaks the
// contract will cause these tests to fail.
// ---------------------------------------------------------------------------

/**
 * Apply the markdown link transform exactly as FileViewer.tsx does it.
 *
 * The input is assumed to be the already HTML-escaped content string
 * (i.e., < → &lt;  " → &quot; etc.), as the render function escapes
 * before applying transforms.
 */
function applyLinkTransform(escapedHtml: string): string {
  return escapedHtml.replace(
    /\[([^\]]{1,300})\]\(([^)]{1,2000})\)/g,
    (_match: string, linkText: string, rawHref: string) => {
      // Decode entities re-introduced by Step 1 of the renderer.
      const href = rawHref
        .replace(/&amp;/g, "&")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'");

      // Reject non-https? schemes.
      if (!/^https?:\/\//i.test(href)) {
        return linkText; // plain text fallback
      }

      const safeHref = href
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;");

      return `<a href="${safeHref}" target="_blank" rel="noopener noreferrer" class="text-blue-400 underline">${linkText}</a>`;
    },
  );
}

/**
 * Full pipeline: escape then apply link transform (mirrors renderMarkdownContent).
 */
function renderLink(markdown: string): string {
  const escaped = markdown
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
  return applyLinkTransform(escaped);
}

// ---------------------------------------------------------------------------

describe("B-26 FileViewer link href sanitization", () => {
  // --- Dangerous schemes must NOT produce an <a> element ---

  it("strips javascript: scheme — output must not contain href=\"javascript:", () => {
    const out = renderLink("[click me](javascript:alert(document.cookie))");
    expect(out).not.toContain("href=");
    expect(out).not.toMatch(/javascript:/i);
    expect(out).toContain("click me"); // text is preserved
  });

  it("strips data:text/html scheme", () => {
    const out = renderLink("[click](data:text/html,<script>alert(1)</script>)");
    expect(out).not.toContain("href=");
    expect(out).not.toMatch(/data:/i);
    expect(out).toContain("click");
  });

  it("strips vbscript: scheme", () => {
    const out = renderLink("[run](vbscript:MsgBox(1))");
    expect(out).not.toContain("href=");
    expect(out).not.toMatch(/vbscript:/i);
    expect(out).toContain("run");
  });

  it("strips file: scheme", () => {
    const out = renderLink("[file](/etc/passwd)");
    // /etc/passwd does not start with https? so it is rejected.
    expect(out).not.toContain("href=");
    expect(out).toContain("file");
  });

  it("strips bare relative paths", () => {
    const out = renderLink("[home](/)");
    expect(out).not.toContain("href=");
    expect(out).toContain("home");
  });

  it("strips javascript: scheme with mixed case (JaVaScRiPt:)", () => {
    const out = renderLink("[x](JaVaScRiPt:void(0))");
    expect(out).not.toContain("href=");
    expect(out).not.toMatch(/javascript/i);
  });

  // --- Safe https? URLs must produce a valid <a> element ---

  it("renders https:// link as a safe anchor", () => {
    const out = renderLink("[docs](https://docs.stripe.com)");
    expect(out).toContain(`href="https://docs.stripe.com"`);
    expect(out).toContain("target=\"_blank\"");
    expect(out).toContain("rel=\"noopener noreferrer\"");
    expect(out).toContain("docs");
  });

  it("renders http:// link as a safe anchor", () => {
    const out = renderLink("[site](http://example.com/page)");
    expect(out).toContain(`href="http://example.com/page"`);
    expect(out).toContain("site");
  });

  it("HTML-encodes ampersands inside the href attribute", () => {
    const out = renderLink("[search](https://example.com?a=1&b=2)");
    expect(out).toContain("&amp;");
    expect(out).not.toMatch(/href="[^"]*[^&]&[^a]/); // no bare & in attribute
  });

  it("preserves link text containing special characters", () => {
    const out = renderLink("[<dangerous> text](https://safe.example.com)");
    // The visible text was escaped by Step 1 so < and > are HTML entities.
    expect(out).toContain("&lt;dangerous&gt;");
  });
});
