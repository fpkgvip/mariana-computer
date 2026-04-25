# Changelog

All notable changes to the Deft frontend.

## 1.0.0 — 2026-04-26 — Deft rebuild (P10–P24)

The 24-phase rebuild that took Deft from a generic AI-app SaaS shell to a focused
"AI developer that doesn't leave you debugging" product. Branch: `feat/deft-rebuild`.

### Marketing + brand

- **P10–P11.** Rebrand to Deft. New hero, new thesis: "an AI developer with a real
  computer." `BRAND` tokens centralized in `lib/brand.ts`. All forbidden hype words
  removed (no ship/supercharge/seamless/AI-powered/etc.).
- **P15.** Marketing copy v2 — Product hero rewritten on-thesis, Contact tightened
  to "Talk to a human" with one-business-day promise. CTAs route `/chat → /signup`.
- **P22.** Final copy audit. Replaced terse "Not authenticated" toasts with
  "Sign in to continue" plain-language pairs across Account, Chat, Checkout,
  Pricing, Skills, InvestigationGraph.

### Pricing + billing

- **P16.** Pricing realigned to locked plan tiers (Starter $29 / Standard $99 /
  Pro $299 / Scale $699). Margin-honest annotation: every plan shows compute at
  1 credit = $0.01. New `BuyCreditsDialog` (radio-list, Stripe disclosure,
  in-page modal) replaces the standalone `/checkout` redirect.

### Errors + observability

- **P13.** Error / empty / loading state primitives with calm voice
  (`ErrorState`, `EmptyState`, `LoadingRows`). `ApiError` now carries
  `x-request-id`. Adopted in ProjectsSidebar, Tasks, Skills, TaskDetail.
- **P17.** Sentry-shaped breadcrumb ring buffer (50 entries, `lib/observability.ts`).
  `errorToast` helper with prefilled "Report issue" mailto containing breadcrumbs
  + route + release. `AppErrorBoundary` captures + offers Report issue. PostHog
  user identification on session sync.

### Build / E2E

- **P14.** Build → Live E2E Playwright smoke (8-stage walk: signup, login, prompt,
  preflight, start/Compile, cancel, resume, receipt/Live). 8/8 green.

### Accessibility, performance, mobile, SEO

- **P18.** WCAG AA pass. New `--accent-strong` token (dark `254 65% 72%`,
  light `254 60% 38%`) for small accent text on muted backgrounds. Stripped
  `opacity-50/60/70` from text contexts that fell below 4.5:1. Axe scan: 0
  serious / 0 critical violations across all surfaces.
- **P19.** Route-level code splitting. 21 routes wrapped in `React.lazy`. Initial
  JS dropped from **1.31 MB (gzip 386 KB) to 65 KB (gzip 17 KB)**.
- **P20.** Mobile pass at 360–375px. Account `BalanceCard` + `PlanCard` stack
  with `flex-col gap-3 sm:flex-row`. Zero horizontal overflow at 360px across
  12 surfaces.
- **P21.** SEO + per-route metadata. Custom `usePageHead` hook (no react-helmet
  dependency) sets `document.title`, meta description, OG/Twitter, canonical
  with restore-on-unmount. `sitemap.xml` covering 6 marketing routes.

### Final test pass

- **P23.** Comprehensive E2E test → debug loop. Three harnesses:
  `p23_comprehensive` (7 public + 10 protected + 21 dev routes + axe regression
  on 6 surfaces), `p23_chaos` (3 randomized fast-click passes across 11
  surfaces + dialog / drawer / wizard stress), `p23_prod_smoke` (production
  build title + console + pageerror gate).
  - **Caught and fixed a real production crash:** circular vendor-react ↔
    catchall vendor- chunk import that broke module init order with
    "Cannot read properties of undefined (reading 'createContext')". Fixed by
    simplifying `manualChunks` to only carve out independent heavy vendors.
  - **Added `retryImport` wrapper** around `React.lazy()` so chunk fetch
    failures (transient network or stale-deploy hash) retry once, then reload
    once on a second failure (with a session-flag guard against reload loops).
  - **Final state:** 0 bugs across all 3 harnesses, dev + production.

### Release

- **P24.** Version bumped to 1.0.0. Changelog created. Branch
  `feat/deft-rebuild` ready for review.
