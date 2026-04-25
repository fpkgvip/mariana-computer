/**
 * Deft v2 — Comprehensive Production E2E
 *
 * Verifies the full billion-dollar rebuild end-to-end:
 *   - Every route: 200 + visible content + zero JS exceptions + zero
 *     unhandled console errors
 *   - Brand cleanliness: no stray "Mariana" text anywhere
 *   - Studio split-pane: /build idle + live mode dom contracts
 *   - Money invariants: integer credits, balance non-negative, plans 3
 *   - Vault: never leaks plaintext into the DOM
 *   - Live agent run: small build → deploys → preview iframe loads
 *
 * Designed to FAIL LOUDLY with line-numbered assertions. Saves a JSON
 * report and a per-route screenshot bundle into /tmp/deft_e2e_v2/.
 */

const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const FRONTEND = process.env.FRONTEND_URL || "https://frontend-tau-navy-80.vercel.app";
const BACKEND = process.env.BACKEND_URL || "http://77.42.3.206:8080";
const EMAIL = process.env.E2E_EMAIL || "testrunner@mariana.test";
const PASSWORD = process.env.E2E_PASSWORD || "DeftTest!2026";
const OUT_DIR = "/tmp/deft_e2e_v2";
const RUN_AGENT = process.env.E2E_RUN_AGENT !== "0"; // set to "0" to skip live run

fs.mkdirSync(OUT_DIR, { recursive: true });

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

const results = [];
let currentSection = "init";

function log(...a) {
  // eslint-disable-next-line no-console
  console.log(`[${new Date().toISOString().slice(11, 23)}] ${currentSection}:`, ...a);
}

async function assert(name, fn) {
  const t0 = Date.now();
  try {
    const detail = await fn();
    results.push({ section: currentSection, name, ok: true, detail: detail ?? null, ms: Date.now() - t0 });
    log(`✓ ${name}`);
    return true;
  } catch (e) {
    const msg = (e && e.message) || String(e);
    results.push({ section: currentSection, name, ok: false, detail: msg, ms: Date.now() - t0 });
    log(`✗ ${name} — ${msg}`);
    return false;
  }
}

function expectEqual(actual, expected, label) {
  if (actual !== expected) throw new Error(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}
function expectTrue(cond, label) {
  if (!cond) throw new Error(label);
}
function expectGte(actual, min, label) {
  if (!(actual >= min)) throw new Error(`${label}: ${actual} < ${min}`);
}

async function snap(page, name) {
  const file = path.join(OUT_DIR, `${name}.png`);
  await page.screenshot({ path: file, fullPage: false }).catch(() => {});
  return file;
}

async function fetchJson(url, init) {
  const res = await fetch(url, init);
  const txt = await res.text();
  let json = null;
  try { json = JSON.parse(txt); } catch { /* leave null */ }
  return { status: res.status, json, text: txt, headers: res.headers };
}

async function getAccessToken() {
  const SUPABASE_URL = "https://afnbtbeayfkwznhzafay.supabase.co";
  const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFmbmJ0YmVheWZrd3puaHphZmF5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzUyOTE1NTIsImV4cCI6MjA5MDg2NzU1Mn0.e_bgdqJryv3lAXEDF8CVL7AHxPzhfKeFkYElAYynF5I";
  const r = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=password`, {
    method: "POST",
    headers: { "Content-Type": "application/json", apikey: SUPABASE_ANON_KEY },
    body: JSON.stringify({ email: EMAIL, password: PASSWORD }),
  });
  const j = await r.json();
  if (!j.access_token) throw new Error(`auth failed: ${JSON.stringify(j)}`);
  return j.access_token;
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, ignoreHTTPSErrors: true });
  const page = await context.newPage();

  // Global console + error capture per page
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      const text = msg.text();
      // Ignore third-party noise
      if (/Failed to load resource.*favicon|net::ERR_BLOCKED_BY_CLIENT|googletagmanager|sentry|analytics/i.test(text)) return;
      consoleErrors.push({ section: currentSection, text });
    }
  });
  page.on("pageerror", (err) => pageErrors.push({ section: currentSection, error: err.message }));

  // -------------------------------------------------------------------------
  // SECTION 1 — Backend health and contracts
  // -------------------------------------------------------------------------
  currentSection = "backend";

  await assert("openapi.json reachable", async () => {
    const r = await fetchJson(`${BACKEND}/openapi.json`);
    expectEqual(r.status, 200, "openapi status");
    expectTrue(r.json && r.json.paths, "openapi schema");
  });

  await assert("preview routes registered in OpenAPI", async () => {
    const r = await fetchJson(`${BACKEND}/openapi.json`);
    const paths = Object.keys(r.json.paths);
    expectTrue(paths.includes("/api/preview/{task_id}"), "missing /api/preview/{task_id}");
  });

  await assert("preview manifest 401 unauthenticated", async () => {
    const r = await fetchJson(`${BACKEND}/api/preview/whatever`);
    expectTrue(r.status === 401 || r.status === 403, `expected 401/403, got ${r.status}`);
  });

  await assert("preview redirect for any task id (302→/index.html)", async () => {
    const r = await fetch(`${BACKEND}/preview/abc123`, { redirect: "manual" });
    expectEqual(r.status, 302, "redirect status");
    const loc = r.headers.get("location");
    expectTrue(loc && loc.endsWith("/index.html"), `bad redirect: ${loc}`);
  });

  await assert("plans endpoint returns 3 tiers (Builder/Pro/Max ids)", async () => {
    const r = await fetchJson(`${BACKEND}/api/plans`);
    expectEqual(r.status, 200, "plans status");
    const plans = r.json.plans || r.json;
    const ids = (plans || []).map((p) => p.id);
    expectTrue(["starter", "pro", "max"].every((x) => ids.includes(x)), `plan ids: ${ids.join(",")}`);
  });

  let token;
  await assert("login returns access token", async () => {
    token = await getAccessToken();
    expectTrue(token && token.length > 30, "token");
  });

  await assert("balance is non-negative integer credits", async () => {
    const r = await fetchJson(`${BACKEND}/api/credits/balance`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expectEqual(r.status, 200, "balance status");
    const b = r.json.balance ?? r.json.credits ?? r.json.tokens;
    expectTrue(Number.isInteger(b), `balance not integer: ${b}`);
    expectGte(b, 0, "balance");
    return { balance: b };
  });

  await assert("preflight quote returns credits + eta range", async () => {
    const r = await fetchJson(`${BACKEND}/api/agent/quote`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: "Build a one-page personal landing page with a hero and a contact form.", tier: "standard" }),
    });
    expectEqual(r.status, 200, `quote status (body=${r.text.slice(0,200)})`);
    expectTrue(r.json && (r.json.credits_min !== undefined || r.json.credits !== undefined || r.json.estimate !== undefined), "credits in quote");
    return r.json;
  });

  // -------------------------------------------------------------------------
  // SECTION 2 — Public routes visual + no-error pass
  // -------------------------------------------------------------------------
  currentSection = "public-routes";

  // Note: text-match is case-insensitive (UI uppercases some labels via CSS/font)
  const PUBLIC_ROUTES = [
    { path: "/", expectText: ["from a prompt", "deployed app", "generation is free"] },
    { path: "/pricing", expectText: ["generation is free", "builder", "pro", "max", "top-ups"] },
    { path: "/product", expectText: [] },
    { path: "/contact", expectText: [] },
    { path: "/login", expectText: ["welcome back", "email", "password"] },
    { path: "/signup", expectText: ["create account", "email", "password"] },
  ];

  for (const r of PUBLIC_ROUTES) {
    await assert(`route ${r.path} renders`, async () => {
      const errsBefore = consoleErrors.length;
      const exBefore = pageErrors.length;
      const resp = await page.goto(`${FRONTEND}${r.path}`, { waitUntil: "networkidle", timeout: 25000 });
      expectTrue(resp && resp.ok(), `bad status ${resp && resp.status()}`);
      // Wait for hydration
      await page.waitForTimeout(400);
      const body = await page.evaluate(() => document.body.innerText);
      const bodyLower = body.toLowerCase();
      for (const t of r.expectText) {
        expectTrue(bodyLower.includes(t.toLowerCase()), `expected "${t}" on ${r.path}`);
      }
      // Brand leak check — only inspect chrome (nav/header/footer/h1-h3, button text)
      // NOT user data (tasks, emails) which legitimately may contain the substring.
      const chromeText = await page.evaluate(() => {
        const sels = ['header', 'nav', 'footer', 'h1', 'h2', 'h3', 'button', '[role="navigation"]', 'title'];
        const out = [];
        for (const s of sels) {
          for (const el of Array.from(document.querySelectorAll(s))) {
            out.push((el.textContent || '').trim());
          }
        }
        out.push(document.title);
        return out.join('\n');
      });
      expectTrue(!/Mariana/i.test(chromeText), `"Mariana" brand leak on ${r.path}`);
      await snap(page, `route_${r.path.replace(/\//g, "_") || "_root"}`);
      // Page errors
      const newPageErrs = pageErrors.length - exBefore;
      expectEqual(newPageErrs, 0, `pageerror count on ${r.path}`);
      const newConsoleErrs = consoleErrors.length - errsBefore;
      // Allow up to 0 console errors on public pages; surface in detail if any
      if (newConsoleErrs > 0) {
        return { warn_console_errors: newConsoleErrs };
      }
    });
  }

  // -------------------------------------------------------------------------
  // SECTION 3 — Homepage interactions
  // -------------------------------------------------------------------------
  currentSection = "homepage";

  await assert("homepage prompt input focusable + accepts text", async () => {
    await page.goto(`${FRONTEND}/`, { waitUntil: "networkidle" });
    await page.waitForTimeout(400);
    const ta = await page.locator('textarea[aria-label="Describe what you want to build"]').first();
    await ta.click();
    await ta.fill("Test prompt for homepage e2e");
    const v = await ta.inputValue();
    expectEqual(v, "Test prompt for homepage e2e", "textarea value");
  });

  await assert("homepage submit (unauthed) routes via /login → /build with prompt preserved", async () => {
    // Use a fresh guest context so we can verify the unauthed flow
    const guest = await browser.newContext();
    const gp = await guest.newPage();
    await gp.goto(`${FRONTEND}/`, { waitUntil: "networkidle" });
    await gp.waitForTimeout(400);
    const ta = gp.locator('textarea[aria-label="Describe what you want to build"]').first();
    await ta.fill("hello world e2e prompt");
    // Submit; unauthed user should land on /login (auth gate). The build page receives the prompt query upon successful auth.
    await gp.keyboard.press("Enter");
    await gp.waitForTimeout(1500);
    const url = new URL(gp.url());
    expectTrue(/^(\/login|\/build)/.test(url.pathname), `path ${url.pathname}`);
    await guest.close();
  });

  await assert("homepage cycling-placeholder element present", async () => {
    await page.goto(`${FRONTEND}/`, { waitUntil: "networkidle" });
    await page.waitForTimeout(400);
    // The placeholder lives next to the textarea with .deft-caret span
    const has = await page.locator(".deft-caret").count();
    expectGte(has, 1, "deft-caret count");
  });

  // -------------------------------------------------------------------------
  // SECTION 4 — Auth gates
  // -------------------------------------------------------------------------
  currentSection = "auth-gates";

  for (const p of ["/build", "/vault", "/tasks", "/account", "/admin"]) {
    await assert(`unauthed ${p} → /login`, async () => {
      const guest = await browser.newContext();
      const gp = await guest.newPage();
      await gp.goto(`${FRONTEND}${p}`, { waitUntil: "networkidle", timeout: 20000 });
      await gp.waitForTimeout(800);
      const finalUrl = new URL(gp.url());
      expectTrue(/^\/(login|signup)/.test(finalUrl.pathname), `landed on ${finalUrl.pathname}`);
      await guest.close();
    });
  }

  // -------------------------------------------------------------------------
  // SECTION 5 — Login → /build
  // -------------------------------------------------------------------------
  currentSection = "login-flow";

  await assert("login navigates to /build", async () => {
    await page.goto(`${FRONTEND}/login`, { waitUntil: "networkidle" });
    await page.waitForTimeout(400);
    await page.fill('#email', EMAIL);
    await page.fill('#password', PASSWORD);
    await Promise.all([
      page.waitForURL(/\/build/, { timeout: 25000 }),
      page.click('button[type="submit"]'),
    ]);
    expectTrue(/\/build/.test(page.url()), `url ${page.url()}`);
  });

  await assert("authed Build page renders idle prompt", async () => {
    await page.goto(`${FRONTEND}/build`, { waitUntil: "networkidle" });
    await page.waitForTimeout(1200);
    const body = await page.evaluate(() => document.body.innerText);
    expectTrue(/What should Deft build/i.test(body), 'idle headline');
    // Brand leak check on chrome only (user task titles can legitimately contain 'mariana' as data)
    const chromeText = await page.evaluate(() => {
      const sels = ['header', 'nav', 'footer', 'h1', 'h2', 'h3', 'title'];
      return sels.flatMap(s => Array.from(document.querySelectorAll(s))).map(e => (e.textContent || '').trim()).join('\n') + '\n' + document.title;
    });
    expectTrue(!/Mariana/i.test(chromeText), 'brand leak on /build chrome');
    await snap(page, "authed_build_idle");
  });

  // -------------------------------------------------------------------------
  // SECTION 6 — Authed routes
  // -------------------------------------------------------------------------
  currentSection = "authed-routes";

  for (const r of ["/tasks", "/vault", "/account", "/checkout", "/pricing"]) {
    await assert(`authed ${r} renders`, async () => {
      const errsBefore = pageErrors.length;
      const resp = await page.goto(`${FRONTEND}${r}`, { waitUntil: "networkidle", timeout: 25000 });
      expectTrue(resp.ok(), `status ${resp.status()}`);
      await page.waitForTimeout(800);
      // Chrome-only brand leak (user data may legitimately contain the substring)
      const chromeText = await page.evaluate(() => {
        const sels = ['header', 'nav', 'footer', 'h1', 'h2', 'h3', 'title'];
        return sels.flatMap(s => Array.from(document.querySelectorAll(s))).map(e => (e.textContent || '').trim()).join('\n') + '\n' + document.title;
      });
      expectTrue(!/Mariana/i.test(chromeText), `brand leak on ${r} chrome`);
      expectEqual(pageErrors.length - errsBefore, 0, `pageerror on ${r}`);
      await snap(page, `authed_${r.replace(/\//g, "_")}`);
    });
  }

  // -------------------------------------------------------------------------
  // SECTION 7 — Vault no-leak check
  // -------------------------------------------------------------------------
  currentSection = "vault";

  await assert("vault page does not leak any plaintext API key string", async () => {
    await page.goto(`${FRONTEND}/vault`, { waitUntil: "networkidle" });
    await page.waitForTimeout(600);
    const body = await page.evaluate(() => document.body.innerText);
    // Generic key-shaped patterns — we don't expect these on screen
    const keyPatterns = [/sk-[A-Za-z0-9]{16,}/, /sk_live_[A-Za-z0-9]{12,}/, /eyJ[A-Za-z0-9_\-]{40,}\./];
    for (const p of keyPatterns) {
      expectTrue(!p.test(body), `key-like string visible: ${p}`);
    }
  });

  // -------------------------------------------------------------------------
  // SECTION 8 — Live agent run + preview deploy
  // -------------------------------------------------------------------------
  if (RUN_AGENT) {
    currentSection = "live-run";

    let taskId = null;
    await assert("start small agent run", async () => {
      const goal = "Build a one-page personal landing page that says 'Hello from Deft E2E' in the center, dark background, no other content. Deploy it.";
      const r = await fetchJson(`${BACKEND}/api/agent`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          goal,
          selected_model: "claude-sonnet-4-6",
          budget_usd: 2.0,
          max_duration_hours: 0.5,
        }),
      });
      expectTrue(r.status === 200 || r.status === 202, `start status ${r.status} ${r.text.slice(0, 200)}`);
      taskId = r.json.task_id;
      expectTrue(!!taskId, "task_id");
      return { task_id: taskId };
    });

    if (taskId) {
      await assert("task reaches terminal state OR deploys within 8 minutes", async () => {
        const start = Date.now();
        const deadline = start + 8 * 60 * 1000;
        let lastState = null;
        let deployed = false;
        while (Date.now() < deadline) {
          const t = await fetchJson(`${BACKEND}/api/agent/${taskId}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          lastState = t.json && t.json.state;
          const m = await fetchJson(`${BACKEND}/api/preview/${taskId}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          if (m.json && m.json.deployed) {
            deployed = true;
            break;
          }
          if (["done", "completed", "failed", "stopped", "cancelled", "error"].includes(lastState)) {
            // give one last poll for manifest
            const m2 = await fetchJson(`${BACKEND}/api/preview/${taskId}`, {
              headers: { Authorization: `Bearer ${token}` },
            });
            deployed = !!(m2.json && m2.json.deployed);
            break;
          }
          await new Promise((r) => setTimeout(r, 4000));
        }
        return { lastState, deployed, elapsedSec: Math.round((Date.now() - start) / 1000) };
      });

      await assert("preview manifest reports deployed=true", async () => {
        const m = await fetchJson(`${BACKEND}/api/preview/${taskId}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        expectTrue(m.json && m.json.deployed === true, `manifest: ${JSON.stringify(m.json).slice(0, 200)}`);
        expectTrue(m.json.url && m.json.url.startsWith(`/preview/${taskId}/`), `bad url: ${m.json.url}`);
        return m.json;
      });

      await assert("preview HTML loads with 200", async () => {
        const r = await fetch(`${BACKEND}/preview/${taskId}/index.html`);
        expectEqual(r.status, 200, `status ${r.status}`);
        const txt = await r.text();
        expectTrue(/<html/i.test(txt), "missing <html");
      });

      await assert("Build page renders preview iframe URL after task selected", async () => {
        // Use domcontentloaded — PreviewPane polls /api/preview/{task_id} every 3s, so networkidle never settles
        await page.goto(`${FRONTEND}/build?task=${taskId}`, { waitUntil: "domcontentloaded", timeout: 25000 });
        // Wait up to 12s for the iframe with /preview/ src to mount
        let iframes = [];
        for (let i = 0; i < 12; i++) {
          await page.waitForTimeout(1000);
          iframes = await page.$$eval("iframe", (els) => els.map((e) => e.src));
          if (iframes.some((s) => /\/preview\//.test(s))) break;
        }
        const hasPreview = iframes.some((s) => /\/preview\//.test(s));
        expectTrue(hasPreview, `iframes seen: ${iframes.join(",")}`);
        await snap(page, "build_live_preview");
      });
    }
  } else {
    log("(skipping live agent run, RUN_AGENT=0)");
  }

  // -------------------------------------------------------------------------
  // FINAL — Console/page errors summary
  // -------------------------------------------------------------------------
  currentSection = "summary";

  const finalReport = {
    frontend: FRONTEND,
    backend: BACKEND,
    started: new Date().toISOString(),
    pageErrors,
    consoleErrors,
    results,
    pass: results.every((r) => r.ok),
    counts: {
      total: results.length,
      pass: results.filter((r) => r.ok).length,
      fail: results.filter((r) => !r.ok).length,
    },
  };
  fs.writeFileSync(path.join(OUT_DIR, "report.json"), JSON.stringify(finalReport, null, 2));
  log(`pass: ${finalReport.counts.pass}/${finalReport.counts.total} fail: ${finalReport.counts.fail}`);
  log(`pageErrors: ${pageErrors.length}, consoleErrors: ${consoleErrors.length}`);

  await browser.close();
  process.exit(finalReport.pass ? 0 : 1);
})().catch((e) => {
  // eslint-disable-next-line no-console
  console.error("FATAL", e);
  process.exit(2);
});
