# P2 Frontend Cluster Report — B-25 through B-29

**Date:** 2026-05-12  
**Branch:** `loop6/zero-bug`  
**Engineer:** subagent P2-frontend  
**Test count before:** 51  **Test count after:** 115  

---

## Summary

Fixed all five P2 frontend bugs in `frontend/src/`. All fixes operate strictly within `frontend/src/`; no backend Python, migrations, or other clusters were touched. Full vitest suite green at 115 tests.

---

## B-25 — Billing portal open redirect (A4-01)

**Root cause:** `Account.tsx` `handleManageSubscription` navigated to `data.portal_url` without validating the URL. `Checkout.tsx` already had the correct allow-list guard.

**Fix:** Added URL validation in `Account.tsx` immediately before the navigation:

```ts
const parsed = new URL(data.portal_url);
const isSameOrigin = parsed.origin === window.location.origin;
const isStripe = parsed.hostname.endsWith(".stripe.com");
if (!isSameOrigin && !isStripe) {
  toast.error("Invalid billing portal URL received from server", { description: msg });
  setIsOpeningPortal(false);
  return;
}
```

**Files modified:**
- `frontend/src/pages/Account.tsx` — lines 229–245 (added validation block)

**Tests added:** `src/test/b25_billing_portal_redirect.test.ts` — 12 tests  
- Allows `billing.stripe.com`, `*.stripe.com`, same-origin  
- Blocks `evil.com`, `javascript:`, `data:`, Stripe-suffix spoofs, empty string, unparseable URL

---

## B-26 — FileViewer markdown XSS via link hrefs (A4-05)

**Root cause:** `FileViewer.tsx` `renderMarkdownContent` had no `[text](url)` link transform at all. Any future copy of a link feature from `Chat.tsx` without the https guard would introduce XSS.

**Fix:** Added a full link transform (Step 3 of the render pipeline) with a strict `https?://` allow-list. Non-http schemes are reduced to plain visible text. Correct entity-decode/re-encode cycle handles HTML-escaped URLs from Step 1.

**Files modified:**
- `frontend/src/components/FileViewer.tsx` — lines 123–151 (new link transform)

**Tests added:** `src/test/b26_fileviewer_markdown_xss.test.ts` — 10 tests  
- `javascript:`, `data:`, `vbscript:`, `file:`, relative paths — all stripped  
- `https://` and `http://` links render as safe anchors  
- Ampersand encoding in href, special chars in link text

---

## B-27 — PreviewPane allow-same-origin removes sandbox (A4-06)

**Root cause:** `PreviewPane.tsx` had `sandbox="allow-scripts allow-same-origin ..."`. Combining these two flags on same-origin user-generated content is equivalent to no sandbox — iframe JS can read parent localStorage, cookies, and DOM.

**Fix (Option B stopgap):** Removed `allow-same-origin` from the sandbox attribute.

New value: `sandbox="allow-scripts allow-forms allow-modals allow-popups allow-downloads"`

**Files modified:**
- `frontend/src/components/deft/PreviewPane.tsx` — line 290 (sandbox attribute)

**Tests added:** `src/test/b27_preview_pane_sandbox.test.ts` — 7 tests  
- Asserts sandbox does not contain `allow-same-origin`  
- Asserts sandbox does still include `allow-scripts` and `allow-forms`  
- Asserts dangerous combination `allow-scripts + allow-same-origin` is absent  
- Snapshot test for exact expected sandbox value

**Follow-up for orchestrator:** Option A (serving previews from `preview.deft.computer`) is the long-term fix. Requires backend routing + vercel.json subdomain rewrite + CSP `frame-src` update.

---

## B-28 — AuthContext infinite spinner on Supabase outage (A4-07)

**Root cause:** `AuthProvider.tsx` had no timeout on the `loading=true` state. If Supabase `onAuthStateChange` never fires, the full-screen spinner persists forever.

**Fix:**
- Exported `AUTH_LOADING_TIMEOUT_MS = 10000` (configurable via `VITE_AUTH_TIMEOUT_MS`)
- Registered a `setTimeout` in the existing `useEffect` alongside `onAuthStateChange`
- Timeout callback: `setLoading(false)`, `setAuthTimedOut(true)`, `setUser(null)`
- `onAuthStateChange` listener: `clearTimeout(timeoutId)` before `syncSession`
- Cleanup: `clearTimeout(timeoutId)` in the effect teardown
- Added `authTimedOut` state rendering a `data-testid="auth-timeout"` error screen with a Retry button
- Updated loading spinner with `role="status"`, `aria-live="polite"`, `aria-label`, `data-testid="auth-loading"`

**Files modified:**
- `frontend/src/contexts/AuthContext.tsx` — lines 91–97 (constant), 102–103 (state), 162–194 (useEffect with timeout), 297–333 (loading/timeout UI)

**Tests added:** `src/test/b28_auth_loading_timeout.test.ts` — 15 tests  
- Constant export, default value, env-var configurability  
- Timeout sets loading=false + authTimedOut=true  
- clearTimeout called in cleanup and on resolution  
- data-testid, role, aria-live, Retry button presence

---

## B-29 — Checkout success pages never refresh credits (A4-08)

**Root cause:** Stripe redirects to `/chat?checkout=success`, `/build?checkout=success`, `/account?topup=success`. None of these pages read the query param or refreshed the credit balance. Users saw stale balances post-payment.

**Fix:** Added a mount-only `useEffect` on each landing page:
1. Detect `?checkout=success` / `?topup=success`
2. Clear the query param (`replace: true` so back-button is safe)
3. Show `toast.success("Payment received", { description: "Credits are updating..." })`
4. Poll `refreshUser()` (and `refetchBalance()` on Build) up to 3× at 3 s intervals

Also: `Build.tsx` now destructures `refreshUser` from `useAuth()`. `Account.tsx` and `Chat.tsx` import `useSearchParams` from react-router-dom.

**Files modified:**
- `frontend/src/pages/Chat.tsx` — import `useSearchParams`, lines 457–487 (checkout effect)
- `frontend/src/pages/Build.tsx` — `refreshUser` destructured, lines 71–99 (checkout effect)
- `frontend/src/pages/Account.tsx` — import `useSearchParams`, `refreshUser` destructured, lines 97–124 (topup effect)

**Tests added:** `src/test/b29_post_checkout_credit_refresh.test.ts` — 20 tests  
- 3 describe blocks (Chat, Build, Account)  
- Each: detects param, calls refresh, shows toast, clears URL, uses replace:true, polls 3×

---

## Test Suite Results

```
Test Files  11 passed (11)
     Tests  115 passed (115)
  Duration  7.82s
```

No pre-existing test was broken. All 64 new tests pass.

---

## Files Changed

| File | Change |
|------|--------|
| `frontend/src/pages/Account.tsx` | B-25: URL allow-list guard; B-29: topup success handler |
| `frontend/src/components/FileViewer.tsx` | B-26: link transform with https allow-list |
| `frontend/src/components/deft/PreviewPane.tsx` | B-27: remove allow-same-origin from sandbox |
| `frontend/src/contexts/AuthContext.tsx` | B-28: 10s timeout, authTimedOut state, accessible UI |
| `frontend/src/pages/Chat.tsx` | B-29: checkout success handler |
| `frontend/src/pages/Build.tsx` | B-29: checkout success handler + refreshUser destructure |
| `frontend/src/test/b25_billing_portal_redirect.test.ts` | 12 tests |
| `frontend/src/test/b26_fileviewer_markdown_xss.test.ts` | 10 tests |
| `frontend/src/test/b27_preview_pane_sandbox.test.ts` | 7 tests |
| `frontend/src/test/b28_auth_loading_timeout.test.ts` | 15 tests |
| `frontend/src/test/b29_post_checkout_credit_refresh.test.ts` | 20 tests |
| `loop6_audit/REGISTRY.md` | B-25..B-29 marked FIXED in dedup table and DAG sections |

---

## Open Questions for Orchestrator

1. **B-27 Option A**: Move preview to `preview.deft.computer` subdomain for full isolation. Requires backend routing, vercel.json rewrite, and CSP `frame-src` update (B-10 already addressed CSP). This is the correct long-term fix; the `allow-same-origin` removal is a safe stopgap.

2. **B-28 timeout value**: 10 s is a reasonable default. If Supabase cold-start on free tier regularly exceeds this, increase `VITE_AUTH_TIMEOUT_MS` in the Vercel environment config without a code change.
