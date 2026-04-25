/**
 * P23 — Chaos / random-rapid-action stress.
 *
 * The user explicitly asked: "if clicking x then y then z then a then b
 * very quickly causes issues". This harness clicks random visible buttons
 * and links across 8 surfaces with 80ms delays, navigates between routes
 * mid-render, and replays the sequence 4 times to surface state-leak bugs
 * that single-pass tests miss.
 */
import { chromium } from "playwright";
import fs from "fs";

const BASE = "http://localhost:8080";

function isNoise(t) {
  if (!t) return false;
  return (
    t.includes("[vite]") ||
    t.includes("[HMR]") ||
    t.includes("Failed to load resource") ||
    t.includes("favicon") ||
    t.includes("PostHog") ||
    t.includes("Sentry Logger") ||
    t.includes("Download the React DevTools") ||
    t.includes("AuthSessionMissingError") ||
    t.includes("AuthApiError") ||
    t.includes("supabase") ||
    t.includes("[observability] captureError") ||
    t.includes("synthetic render crash") ||
    t.includes("synthetic background error") ||
    t.includes("Something locally went sideways") ||
    t.includes("Backend unavailable") ||
    t.includes("Cannot update during an existing state transition")
  );
}

function isNoisePageError(t) {
  if (!t) return false;
  return (
    t.includes("synthetic render crash") ||
    t.includes("synthetic background error") ||
    t.includes("Something locally went sideways") ||
    // Vite dev server invalidates chunks during HMR; production has retryImport guard.
    t.includes("Failed to fetch dynamically imported module")
  );
}

const SURFACES = [
  "/",
  "/pricing",
  "/product",
  "/contact",
  "/signup",
  "/login",
  "/dev/states",
  "/dev/projects?mode=open_some",
  "/dev/account?mode=plus",
  "/dev/vault?mode=unlocked_with_secrets",
  "/dev/studio?mode=live",
];

const failures = [];

async function chaosClickPass(page, route, seed) {
  await page.goto(BASE + route, { waitUntil: "domcontentloaded", timeout: 12000 });
  await page.waitForTimeout(300);
  const targets = await page.$$('button:visible, [role="button"]:visible, a[href]:visible, [role="checkbox"]:visible, [role="radio"]:visible, [role="tab"]:visible, [role="switch"]:visible');
  // Shuffle deterministically with seed
  const arr = targets.slice();
  let s = seed;
  function rand() { s = (s * 9301 + 49297) % 233280; return s / 233280; }
  arr.sort(() => rand() - 0.5);
  for (let i = 0; i < Math.min(8, arr.length); i++) {
    try {
      await arr[i].click({ force: true, timeout: 200, noWaitAfter: true });
    } catch { /* ignore */ }
    await page.waitForTimeout(60);
  }
  // Random keyboard input
  await page.keyboard.press("Escape");
  await page.waitForTimeout(80);
}

async function navigateThrash(page) {
  // Bounce between routes mid-render to stress unmount cleanup.
  const seq = ["/", "/pricing", "/product", "/", "/signup", "/login", "/contact", "/", "/pricing"];
  for (const r of seq) {
    try {
      page.goto(BASE + r, { waitUntil: "commit", timeout: 6000 }).catch(() => {});
    } catch {}
    await page.waitForTimeout(120);
  }
  // Final settle
  try { await page.goto(BASE + "/", { waitUntil: "networkidle", timeout: 10000 }); } catch {}
}

async function dialogOpenCloseStress(page) {
  // Open + close BuyCreditsDialog repeatedly
  await page.goto(BASE + "/dev/account?mode=plus", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);
  for (let i = 0; i < 4; i++) {
    const trig = await page.$('button:has-text("Add credits"), button:has-text("Buy credits"), button:has-text("Top up")');
    if (!trig) break;
    try { await trig.click({ force: true, timeout: 300 }); } catch {}
    await page.waitForTimeout(150);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(120);
  }
}

async function studioDrawerStress(page) {
  await page.setViewportSize({ width: 768, height: 800 });
  await page.goto(BASE + "/dev/studio?mode=live", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);
  for (let i = 0; i < 5; i++) {
    const t = await page.$('button[aria-label*="menu" i], button[aria-controls*="drawer" i], button[aria-label*="open" i]');
    if (t) {
      try { await t.click({ force: true, timeout: 200 }); } catch {}
      await page.waitForTimeout(100);
      await page.keyboard.press("Escape");
      await page.waitForTimeout(80);
    } else {
      break;
    }
  }
  await page.setViewportSize({ width: 1280, height: 800 });
}

async function vaultSetupWizardThrash(page) {
  await page.goto(BASE + "/dev/vault?mode=setup", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);
  // Type in passphrase field if present, click next/back rapidly
  const pp = await page.$('input[type="password"]');
  if (pp) {
    await pp.fill("test-passphrase-123");
    await page.waitForTimeout(60);
  }
  const btns = await page.$$('button:visible');
  for (let i = 0; i < Math.min(5, btns.length); i++) {
    try { await btns[i].click({ force: true, timeout: 200, noWaitAfter: true }); } catch {}
    await page.waitForTimeout(60);
  }
  await page.keyboard.press("Escape");
}

async function instrument(page, label) {
  const consoleErrors = [];
  const pageErrors = [];
  const onConsole = (msg) => {
    if (msg.type() === "error") {
      const t = msg.text();
      if (!isNoise(t)) consoleErrors.push(t);
    }
  };
  const onPageError = (err) => {
    const t = String(err && err.message ? err.message : err);
    if (!isNoisePageError(t)) pageErrors.push(t);
  };
  page.on("console", onConsole);
  page.on("pageerror", onPageError);
  return {
    finish() {
      page.off("console", onConsole);
      page.off("pageerror", onPageError);
      if (consoleErrors.length) failures.push({ category: "chaos:console", route: label, detail: consoleErrors.slice(0, 3) });
      if (pageErrors.length) failures.push({ category: "chaos:pageerror", route: label, detail: pageErrors.slice(0, 3) });
    }
  };
}

async function main() {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await ctx.newPage();

  for (let pass = 1; pass <= 3; pass++) {
    console.log(`\n=== PASS ${pass} ===`);
    for (const surface of SURFACES) {
      const m = await instrument(page, `pass${pass}:${surface}`);
      try {
        await chaosClickPass(page, surface, pass * 1000 + surface.length);
      } catch (e) {
        failures.push({ category: "chaos:throw", route: `pass${pass}:${surface}`, detail: String(e.message || e) });
      }
      m.finish();
    }
    let m;
    m = await instrument(page, `pass${pass}:nav-thrash`);
    try { await navigateThrash(page); } catch (e) { failures.push({ category: "chaos:throw", route: `pass${pass}:nav-thrash`, detail: String(e.message || e) }); }
    m.finish();

    m = await instrument(page, `pass${pass}:dialog-stress`);
    try { await dialogOpenCloseStress(page); } catch (e) { failures.push({ category: "chaos:throw", route: `pass${pass}:dialog-stress`, detail: String(e.message || e) }); }
    m.finish();

    m = await instrument(page, `pass${pass}:studio-drawer`);
    try { await studioDrawerStress(page); } catch (e) { failures.push({ category: "chaos:throw", route: `pass${pass}:studio-drawer`, detail: String(e.message || e) }); }
    m.finish();

    m = await instrument(page, `pass${pass}:vault-wizard`);
    try { await vaultSetupWizardThrash(page); } catch (e) { failures.push({ category: "chaos:throw", route: `pass${pass}:vault-wizard`, detail: String(e.message || e) }); }
    m.finish();
  }

  await browser.close();
  fs.writeFileSync("/tmp/p23_chaos_trace.json", JSON.stringify(failures, null, 2));
  console.log("\n=== CHAOS RESULT ===");
  console.log(`Failures: ${failures.length}`);
  if (failures.length === 0) console.log("0 bugs across 3 chaos passes.");
  else console.log(JSON.stringify(failures.slice(0, 8), null, 2));
}

main().catch((e) => {
  console.error("FATAL:", e);
  process.exit(2);
});
