/**
 * P23 — Production-build smoke.
 *
 * The dev server forgives module ordering; the production build does not.
 * This test runs against the static dist/ output and asserts:
 *   - All public routes render with no page errors and no console errors.
 *   - usePageHead correctly mutates document.title per route.
 *   - SPA fallback works for arbitrary 404 paths.
 */
import { chromium } from "playwright";

const BASE = process.env.BASE || "http://localhost:8095";

function isNoise(t) {
  if (!t) return false;
  return (
    t.includes("favicon") ||
    t.includes("supabase") ||
    t.includes("PostHog") ||
    t.includes("Sentry") ||
    t.includes("AuthSession") ||
    t.includes("403") ||
    t.includes("Failed to load resource") ||
    t.includes("[observability]") ||
    t.includes("apple-touch-icon")
  );
}

const ROUTES = [
  ["/", "Deft —"],
  ["/pricing", "Pricing —"],
  ["/product", "Product —"],
  ["/contact", "Contact —"],
  ["/login", "Log in —"],
  ["/signup", "Sign up —"],
  ["/this-route-doesnt-exist", "Page not found —"],
];

async function main() {
  const b = await chromium.launch();
  const ctx = await b.newContext();
  const failures = [];

  for (const [route, expectTitlePrefix] of ROUTES) {
    const p = await ctx.newPage();
    const errs = [];
    const peErrs = [];
    p.on("console", (m) => { if (m.type() === "error" && !isNoise(m.text())) errs.push(m.text()); });
    p.on("pageerror", (e) => peErrs.push(String(e.message || e)));
    await p.goto(BASE + route, { waitUntil: "networkidle", timeout: 12000 });
    await p.waitForTimeout(1200);
    const title = await p.title();
    if (!title.startsWith(expectTitlePrefix)) {
      failures.push({ route, kind: "title", got: title, expected: `${expectTitlePrefix}*` });
    }
    if (errs.length) failures.push({ route, kind: "console", detail: errs.slice(0, 2) });
    if (peErrs.length) failures.push({ route, kind: "pageerror", detail: peErrs.slice(0, 2) });
    await p.close();
  }

  await b.close();
  console.log(`Production smoke: ${failures.length === 0 ? "0 bugs" : failures.length + " failures"}`);
  if (failures.length) console.log(JSON.stringify(failures, null, 2));
  process.exit(failures.length === 0 ? 0 : 1);
}

main().catch((e) => { console.error(e); process.exit(2); });
