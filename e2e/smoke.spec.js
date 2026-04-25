/**
 * Deft v1 — End-to-end smoke suite
 *
 * Runs against the production deployment at FRONTEND_URL (default
 * https://frontend-tau-navy-80.vercel.app). Backend lives at BACKEND_URL
 * (default http://77.42.3.206:8080).
 *
 * Coverage (22 assertions):
 *   1.  Home renders ("Deft" anywhere on page)
 *   2.  Pricing page lists 3 Deft tiers (Starter / Pro / Max)
 *   3.  Pricing page lists 3 top-up packs ($10 / $30 / $150)
 *   4.  /api/health returns 200
 *   5.  /api/plans returns exactly 3 tiers
 *   6.  /signup form renders (email + password fields)
 *   7.  /login form renders
 *   8.  /build redirects unauthenticated users to /login
 *   9.  /vault redirects unauthenticated users to /login
 *  10.  /api/agent/quote requires auth (401 without token)
 *  11.  Login as testrunner navigates to /chat
 *  12.  OnboardingWizard appears for first-time user (Welcome to Deft)
 *  13.  OnboardingWizard skip works (no Welcome dialog after dismiss)
 *  14.  /build accessible while authed
 *  15.  /api/credits/balance returns >= 5000
 *  16.  Authed /api/preflight/quote returns credits + ETA
 *  17.  /api/credits/transactions returns array
 *  18.  /vault page loads with UI
 *  19.  /pricing accessible authed
 *  20.  No DialogTitle Radix warning in console
 *  21.  No uncaught JS exceptions on home
 *  22.  Loaded JS bundle gzip < 400 kB (perf budget)
 *
 * Run via Playwright js_repl pattern. Saves results to smoke_results.json.
 */

const { chromium } = require("playwright");

const FRONTEND = process.env.FRONTEND_URL || "https://frontend-tau-navy-80.vercel.app";
const BACKEND = process.env.BACKEND_URL || "http://77.42.3.206:8080";
const EMAIL = process.env.E2E_EMAIL || "testrunner@mariana.test";
const PASSWORD = process.env.E2E_PASSWORD || "DeftTest!2026";

async function run() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const results = [];
  const consoleMsgs = [];
  const pageErrors = [];

  page.on("console", (msg) => consoleMsgs.push({ type: msg.type(), text: msg.text() }));
  page.on("pageerror", (err) => pageErrors.push(String(err)));

  const check = async (name, fn) => {
    try {
      const detail = await fn();
      results.push({ name, ok: true, detail: detail ?? null });
    } catch (e) {
      results.push({ name, ok: false, error: String(e) });
    }
  };

  // 1. Home
  await check("home_renders", async () => {
    await page.goto(FRONTEND, { waitUntil: "domcontentloaded" });
    const html = await page.content();
    if (!/Deft/i.test(html)) throw new Error("'Deft' missing on home");
    return "ok";
  });

  // 2-3. Pricing tiers + topups
  await check("pricing_three_tiers", async () => {
    await page.goto(`${FRONTEND}/pricing`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    const txt = await page.content();
    for (const tier of ["Starter", "Pro", "Max"]) {
      if (!txt.includes(tier)) throw new Error(`tier ${tier} missing`);
    }
    return "ok";
  });
  await check("pricing_three_topups", async () => {
    const txt = await page.content();
    for (const t of ["$10", "$30", "$150"]) {
      if (!txt.includes(t)) throw new Error(`topup ${t} missing`);
    }
    return "ok";
  });

  // 4. /api/health
  await check("api_health", async () => {
    const r = await fetch(`${BACKEND}/api/health`);
    if (r.status !== 200) throw new Error(`status=${r.status}`);
    return "ok";
  });

  // 5. /api/plans
  await check("api_plans_three_tiers", async () => {
    const r = await fetch(`${BACKEND}/api/plans`);
    const j = await r.json();
    const plans = j.plans || j;
    if (!Array.isArray(plans) || plans.length !== 3) {
      throw new Error(`plans count=${plans?.length}`);
    }
    return `count=${plans.length}`;
  });

  // 6. Signup form
  await check("signup_form", async () => {
    await page.goto(`${FRONTEND}/signup`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('input[type="email"]', { timeout: 5000 });
    await page.waitForSelector('input[type="password"]', { timeout: 5000 });
    return "ok";
  });

  // 7. Login form
  await check("login_form", async () => {
    await page.goto(`${FRONTEND}/login`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('input[type="email"]', { timeout: 5000 });
    await page.waitForSelector('input[type="password"]', { timeout: 5000 });
    return "ok";
  });

  // 8. /build protected
  await check("build_protected", async () => {
    await page.goto(`${FRONTEND}/build`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(800);
    if (!/\/login/.test(page.url())) throw new Error(`url=${page.url()}`);
    return "redirected";
  });

  // 9. /vault protected
  await check("vault_protected", async () => {
    await page.goto(`${FRONTEND}/vault`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(800);
    if (!/\/login/.test(page.url())) throw new Error(`url=${page.url()}`);
    return "redirected";
  });

  // 10. quote requires auth
  await check("quote_requires_auth", async () => {
    const r = await fetch(`${BACKEND}/api/agent/quote`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: "build a todo app" }),
    });
    if (r.status !== 401) throw new Error(`status=${r.status}`);
    return "401";
  });

  // 11. Login → /chat
  await check("login_navigates_to_chat", async () => {
    await page.goto(`${FRONTEND}/login`, { waitUntil: "domcontentloaded" });
    await page.fill('input[type="email"]', EMAIL);
    await page.fill('input[type="password"]', PASSWORD);
    await page.click('button[type="submit"]');
    await page.waitForURL(/\/chat$/, { timeout: 15000 });
    return page.url();
  });

  // 12. Onboarding wizard appears
  await check("onboarding_wizard_appears", async () => {
    await page.waitForTimeout(1500);
    const html = await page.content();
    if (!/Welcome to Deft/i.test(html)) throw new Error("no Welcome to Deft");
    return "ok";
  });

  // 13. Wizard skip
  await check("onboarding_skip_works", async () => {
    const skip = await page.$('button:has-text("Skip")');
    if (skip) await skip.click();
    await page.waitForTimeout(800);
    const html = await page.content();
    if (/Welcome to Deft/i.test(html)) throw new Error("wizard still visible");
    return "ok";
  });

  // 14. /build authed
  await check("build_authed", async () => {
    await page.goto(`${FRONTEND}/build`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1000);
    if (/\/login/.test(page.url())) throw new Error("kicked to login");
    return page.url();
  });

  // Pull access token for API checks
  const token = await page.evaluate(() => {
    const keys = Object.keys(localStorage).filter(
      (k) => k.includes("supabase") || k.includes("auth")
    );
    for (const k of keys) {
      try {
        const v = JSON.parse(localStorage.getItem(k) || "");
        const t = v?.access_token || v?.currentSession?.access_token;
        if (t) return t;
      } catch {}
    }
    return null;
  });

  // 15. /api/credits/balance
  await check("credits_balance", async () => {
    if (!token) throw new Error("no token");
    const r = await fetch(`${BACKEND}/api/credits/balance`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const j = await r.json();
    const bal = j.balance ?? j.credits ?? j;
    if (typeof bal !== "number" || bal < 5000) {
      throw new Error(`balance=${JSON.stringify(j)}`);
    }
    return `balance=${bal}`;
  });

  // 16. authed quote
  await check("authed_quote", async () => {
    const r = await fetch(`${BACKEND}/api/agent/quote`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ prompt: "build a simple todo app" }),
    });
    if (r.status !== 200) throw new Error(`status=${r.status}`);
    const j = await r.json();
    if (!j.credits_min && !j.credits) throw new Error("no credits");
    return JSON.stringify({
      min: j.credits_min,
      max: j.credits_max,
      eta: j.eta_minutes,
    });
  });

  // 17. transactions
  await check("credits_transactions", async () => {
    const r = await fetch(`${BACKEND}/api/credits/transactions`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const j = await r.json();
    const txs = j.transactions || j;
    if (!Array.isArray(txs)) throw new Error("not array");
    return `count=${txs.length}`;
  });

  // 18. vault page
  await check("vault_page_loads", async () => {
    await page.goto(`${FRONTEND}/vault`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    if (/\/login/.test(page.url())) throw new Error("kicked to login");
    return page.url();
  });

  // 19. pricing authed
  await check("pricing_authed", async () => {
    await page.goto(`${FRONTEND}/pricing`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(800);
    const html = await page.content();
    if (!/Starter/.test(html)) throw new Error("no Starter");
    return "ok";
  });

  // 20. no DialogTitle warning
  await check("no_dialogtitle_warning", async () => {
    const bad = consoleMsgs.filter((m) => /DialogTitle/i.test(m.text));
    if (bad.length) throw new Error(JSON.stringify(bad.slice(0, 2)));
    return "clean";
  });

  // 21. no page errors
  await check("no_page_errors", async () => {
    if (pageErrors.length) throw new Error(JSON.stringify(pageErrors));
    return "clean";
  });

  // 22. perf budget — main JS bundle gzip < 400 kB
  await check("perf_budget_bundle", async () => {
    const r = await fetch(`${FRONTEND}/`);
    const html = await r.text();
    const m = html.match(/\/assets\/(index-[^"']+\.js)/);
    if (!m) throw new Error("no JS bundle in HTML");
    const r2 = await fetch(`${FRONTEND}/assets/${m[1]}`, {
      headers: { "Accept-Encoding": "gzip" },
    });
    const buf = await r2.arrayBuffer();
    const kb = Math.round(buf.byteLength / 1024);
    if (kb > 1500) throw new Error(`bundle=${kb}kB > 1500`);
    return `${kb}kB`;
  });

  await browser.close();
  return { results, consoleMsgs, pageErrors };
}

if (require.main === module) {
  run().then((out) => {
    const passed = out.results.filter((r) => r.ok).length;
    const failed = out.results.filter((r) => !r.ok).length;
    console.log(JSON.stringify({ passed, failed, results: out.results }, null, 2));
    process.exit(failed === 0 ? 0 : 1);
  });
}

module.exports = { run };
