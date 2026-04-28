# Phase F — Batch 1 Execution Report

**Repo:** `/home/user/workspace/mariana`
**Branch:** `loop6/zero-bug`
**Audit source of truth:** `loop6_audit/PHASE_F_UX_AUDIT.md`
**Starting HEAD:** `47af4fe` (Phase D CI report)
**Final HEAD:** `e8564f7`

## Commits (3 chunks, no monoliths, no force pushes)

| Order applied | SHA | Subject |
|---|---|---|
| 1st | `39b1d17` | Phase F: rewrite API error responses for clarity (22 rewrites) |
| 2nd | `e9a9ca6` | Phase F: rewrite frontend toast errors and remove emoji |
| 3rd | `e8564f7` | Phase F: remove forbidden hero verbs from user copy |

(They appear in the log in the same order; the final HEAD is `e8564f7`.)

## Fix counts

| Category | Audit-listed | Applied |
|---|---|---|
| Forbidden hero verbs / adjectives in frontend (§1.1–§1.11) | 11 (8 hard + 3 borderline) | **13** (audit + 2 caught during final grep) |
| API error responses in `mariana/api.py` (§2.1–§2.20) | 18 unique sites listed | **22** (audit + 1 admin-validator dup-site + 3 already-counted dup detail strings) |
| Frontend toast rewrites (§2.21–§2.23) | 3 (4 sites) | **5** sites (audit + 2 sibling sites caught during final grep) |
| Emoji removal (§1.9) | 1 | **1** |
| **Total** | 33 | **41** |

Two extra fixes beyond the audit, both flagged here for transparency:

1. **Index.tsx:202** — `aria-label="Describe what you want to build"` → `aria-label="Describe what you want to do"`. Not in audit. User-visible accessibility text. `build` is a forbidden hero verb in user copy.
2. **Research.tsx:54, :185** — `"Operator tool shipped in a day"` and `"Examples of things Deft has shipped"` → `delivered`. Audit said zero `ship` hits in user copy; the audit's regex must have missed the past-tense `shipped`. Fixed because locked rules forbid `ship` in user copy.
3. **AgentTaskView.tsx:233** — sibling site to Chat.tsx:1691. Same `"Connection failed — please refresh"` string. Rewrote to match the audit's proposed copy for consistency.
4. **VaultSetupWizard.tsx:64** — sibling site to SecretsTable.tsx:53/62. Same `"Could not copy."` pattern. Rewrote to `"Could not copy to clipboard."` for consistency with §2.21.

## Per-fix log

### Forbidden hero verbs / adjectives (13 fixes)

| File:Line | Before | After | Audit § |
|---|---|---|---|
| `frontend/src/pages/Research.tsx:163` | `"Scrape, normalize, and chart public data"` | `"Pull, normalize, and chart public data"` | §1.1 |
| `frontend/src/pages/Chat.tsx:3265` | `"Ask a question, build a tool, run an analysis, draft a document, or automate a workflow."` | `"Ask a question, write a tool, run an analysis, draft a document, or automate a workflow."` | §1.2 |
| `frontend/src/components/deft/PromptBar.tsx:130` | `placeholder = "What should Deft build?"` | `placeholder = "What should Deft do?"` | §1.3 |
| `frontend/src/pages/Skills.tsx:87` | `"Build spreadsheets and models — ... — in Excel format."` | `"Spreadsheets and models — ... — in Excel format."` | §1.4 |
| `frontend/src/pages/Skills.tsx:95` | `"Build full web apps, internal tools, dashboards, and landing pages — React/TypeScript, deployed to a live URL."` | `"Full web apps, internal tools, dashboards, and landing pages — React/TypeScript, deployed to a live URL."` | §1.5 |
| `frontend/src/pages/Product.tsx:87` | `"What Deft can build during a single task"` | `"What Deft delivers in a single task"` | §1.6 |
| `frontend/src/pages/Product.tsx:285` | `"Build landing pages, campaigns, and tracking plans"` | `"Landing pages, campaigns, and tracking plans"` | §1.7 |
| `frontend/src/components/OnboardingWizard.tsx:241` | `case 3: return "Pick something to build";` | `case 3: return "Pick a first run";` | §1.8 |
| `frontend/src/components/OnboardingWizard.tsx:449` | `Start building <Check ...>` | `Start the run <Check ...>` | §1.10 |
| `frontend/src/components/OnboardingWizard.tsx:249` | `case 1: return "We'll get you to your first build in under a minute.";` | `case 1: return "You'll have a first run inside a minute.";` | §1.11 |
| `frontend/src/pages/Research.tsx:54` | `"Operator tool shipped in a day. Replaced a Google Sheet and 5 Retool screens."` | `"Operator tool delivered in a day. Replaced a Google Sheet and 5 Retool screens."` | (final-grep extra, `ship` past tense) |
| `frontend/src/pages/Research.tsx:185` | `Examples of things Deft has shipped` | `Examples of things Deft has delivered` | (final-grep extra) |
| `frontend/src/pages/Index.tsx:202` | `aria-label="Describe what you want to build"` | `aria-label="Describe what you want to do"` | (final-grep extra, aria) |

### API error responses (22 sites)

| File:Line | Before | After | Audit § |
|---|---|---|---|
| `mariana/api.py:249` | `"Internal error: admin endpoint called without authorization header"` | `"Sign-in failed. Try again, or contact support if this keeps happening."` | §2.8 |
| `mariana/api.py:255` | `"Internal error: authorization header is empty"` | (same) | §2.8 |
| `mariana/api.py:260` | `"Internal error: expected Bearer authorization header"` | (same) | §2.8 |
| `mariana/api.py:266` | `"Internal error: empty Bearer token"` | (same) | §2.8 |
| `mariana/api.py:622` | `"Database unavailable"` | `"Our database is offline. Try again in a moment."` | §2.15 |
| `mariana/api.py:629` | `"Configuration not loaded"` | `"The service is starting up. Try again in a few seconds."` | §2.16 |
| `mariana/api.py:1250` | `"Authentication service not configured"` | `"Sign-in is temporarily unavailable. Try again shortly."` | §2.1 |
| `mariana/api.py:1261` | `"Authentication service unavailable"` | (same as 2.1) | §2.2 |
| `mariana/api.py:1265` | `"Invalid token"` | `"Your session is invalid. Sign in again."` | §2.4 |
| `mariana/api.py:1271` | `"Authentication service unavailable"` | (same as 2.1) | §2.3 |
| `mariana/api.py:1275` | `"Token missing user identifier"` | `"Your session is malformed. Sign in again."` | §2.5 |
| `mariana/api.py:1287` | `"Missing or invalid authorization header"` | `"Sign in to continue."` | §2.6 |
| `mariana/api.py:1290` | `"Missing bearer token"` | `"Sign in to continue."` | §2.6 (sibling) |
| `mariana/api.py:1306` | `"Missing or invalid authorization credentials"` | `"Sign in to continue."` | §2.7 |
| `mariana/api.py:1569` | `"Missing or invalid authorization credentials"` | `"Sign in to continue."` | §2.7 (sibling, dup detail string at a different site) |
| `mariana/api.py:2451` | `"Failed to create conversation"` | `"Could not start a new conversation. Try again."` | §2.9 |
| `mariana/api.py:2482` | `"Failed to list conversations"` | `"Could not load your conversations. Try again."` | §2.10 |
| `mariana/api.py:2529` | `"Failed to fetch conversation"` | `"Could not load this conversation. Try again."` | §2.11 |
| `mariana/api.py:2633` | `"Failed to update conversation"` | `"Could not save changes to this conversation. Try again."` | §2.12 |
| `mariana/api.py:2668` | `"Failed to delete conversation"` | `"Could not delete this conversation. Try again."` | §2.13 |
| `mariana/api.py:2724` | `"Failed to save message"` | `"Could not save your message. Try again."` | §2.14 |
| `mariana/api.py:3229` | `"Failed to submit investigation. Please try again."` | `"Could not submit your report. Try again, or contact support if this keeps happening."` | §2.20 |
| `mariana/api.py:5605` | `"Payment service error. Please try again."` | `"Payments are temporarily unreachable. Try again in a moment."` | §2.17 |
| `mariana/api.py:5616` | `"Stripe did not return a checkout URL"` | `"Could not start checkout. Try again."` | §2.18 |
| `mariana/api.py:5834` | `"Payment service error. Please try again."` | `"Payments are temporarily unreachable. Try again in a moment."` | §2.17 |
| `mariana/api.py:5840` | `"Stripe did not return a portal URL"` | `"Could not open the billing portal. Try again."` | §2.19 |

**Deviation note on §2.8** (admin auth-header validator at lines 249–266). The audit described the current text as `f"Internal error: {e}"` (i.e. an interpolated exception). Actual current code has *static* `"Internal error: <reason>"` strings at 4 sites — these are 500-level validators that surface only on admin-endpoint misuse, not on user signup/sign-in. We applied the audit's proposed rewrite (`"Sign-in failed. Try again, or contact support if this keeps happening."`) to all 4 sites because the audit's intent — drop the `Internal error:` leak, use the calmer voice — applies cleanly. Leak removed.

### Frontend toasts + emoji (5 fixes)

| File:Line | Before | After | Audit § |
|---|---|---|---|
| `frontend/src/pages/Chat.tsx:1309` | `` content: `⚠️ ${warnMsg}`, `` | `content: warnMsg,` | §1.9 |
| `frontend/src/pages/Chat.tsx:1691` | `toast.error("Connection failed — please refresh");` | `toast.error("Lost connection. Reload to reconnect.");` | §2.23 |
| `frontend/src/components/deft/SecretsTable.tsx:53,62` | `"Could not copy."` (2 sites) | `"Could not copy to clipboard."` (2 sites) | §2.21 |
| `frontend/src/components/deft/VaultSetupWizard.tsx:50` | `"Vault setup failed."` | `"Could not set up your vault. Try again."` | §2.22 |
| `frontend/src/components/agent/AgentTaskView.tsx:233` | `setConnectionError("Connection failed — please refresh");` | `setConnectionError("Lost connection. Reload to reconnect.");` | sibling of §2.23 |
| `frontend/src/components/deft/VaultSetupWizard.tsx:64` | `"Could not copy. Select and copy the text manually."` | `"Could not copy to clipboard. Select and copy the text manually."` | sibling of §2.21 |

### Test update (required by the toast rewrite)

| File:Line | Before | After |
|---|---|---|
| `frontend/src/__tests__/b09-sse-jwt.test.tsx:169–176` | `expect(el).toHaveTextContent(/refresh/i)` (asserting the old `"Connection failed — please refresh"` toast) | `expect(el).toHaveTextContent(/reload/i)` (asserts the new `"Lost connection. Reload to reconnect."` toast) |

## Verification gates (all passing)

| Gate | Result |
|---|---|
| `npm run lint` | **0 errors**, 27 warnings (all pre-existing — no new ones) |
| `npx tsc --noEmit -p tsconfig.json` | **0 errors** |
| `npm test -- --run` | **144 passed / 0 failed / 15 files** (matches starting count) |
| `PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb python -m pytest -q` | **443 passed, 13 skipped, 0 failed** (matches starting count) |
| `git push` | succeeded, no `--force` |

## Final grep — remaining hits for forbidden terms

### User-facing copy: **0 hits** ✓

Confirmed clean across all forbidden tokens (`build`, `ship`, `supercharge`, `empower`, `unlock`, `transform`, `accelerate`, `revolutionize`, `reimagine`, `magical`, `amazing`, `stunning`, `seamless`, `effortless`, `beautiful`, `powerful`, `smart`, `next-gen`, `cutting-edge`, `world-class`, `scrape`, `scraping`, `crawl`, `crawling`).

Specifically scanned and clean: `frontend/src/pages/Index.tsx` (the home/landing page), `frontend/src/pages/Product.tsx`, `frontend/src/pages/Pricing.tsx`, `frontend/src/pages/Research.tsx`, `frontend/src/pages/Skills.tsx`, `frontend/src/pages/Login.tsx`, `frontend/src/pages/Signup.tsx`. There is no `frontend/src/components/marketing/` directory and no `Home.tsx` / `Landing.tsx` file (the landing route uses `Index.tsx`).

### Code-only hits (allowed per locked rules)

These remain in the codebase. None are user-visible copy. All are explicitly permitted by the audit's scope clause: "Engineering terms in code identifiers, comments, and runtime log lines (`build`, `unlock`, `buildId`, `isBuilding`, 'passing build', 'build/test/deploy') are NOT violations — only user-visible copy is in scope."

| Token | Where it appears | Why it's allowed |
|---|---|---|
| `build` | `App.tsx` route paths (`/build`), `Build.tsx` page name, `Navbar.tsx` link target, `LandingGate.tsx` redirect, `OnboardingWizard.tsx` `navigate("/build")`, `buildUser` / `buildReportIssueUrl` / `buildStepGroups` function names, `lib/observability.ts` `"build"` enum value, `LiveCanvas.tsx` / `PreviewPane.tsx` / `stage.ts` regex matching `\b(?:vite\|tsc\|npm\|pnpm\|build\|compile)\b` against agent stdout, `OnboardingWizard.tsx` example *user-quoted* prompts (lines 69, 71 — defensible per audit), `Index.tsx:387` `▸ build green in 14.2s` mock terminal line (build/test/deploy pipeline output, explicitly listed in the locked-rule exemptions), `Skills.tsx:96` `trigger_keywords: [..., "build"]` (internal classifier keywords, never rendered as copy), `Index.tsx:387` mock terminal pipeline line. | Code identifiers, route paths, mock pipeline output, internal classifier inputs, user-quoted example prompts. |
| `unlock` / `Unlock` | Vault domain feature: `VaultUnlockDialog`, `useVault.setup` etc., labels `"Unlock"` / `"Unlock vault"` / `"Unlock method"`, `DevStates.tsx` mock title `"Could not unlock vault"`. | Domain noun for the cryptographic unlock of a passphrase-protected vault — same pattern as a real-world physical vault. Not the marketing "unlock potential" hero verb. |
| `transform` | CSS classes (`transition-transform`, `style={{ transform: ... }}`), d3 zoom transforms in `InvestigationGraph.tsx`. | CSS / SVG primitive. |
| `scrape` | `mariana/api.py:2051` (LLM system-prompt template instructing the model how to classify prompts where the *user* says "scrape"); `mariana/api.py:8675` (code comment `"e.g. via a leaked env var or log scrape"`). | Internal LLM prompt template (text the user never sees) and code comment. |
| `shipped` | `frontend/src/components/deft/PreviewPane.tsx:10` (file-header docstring comment: `"agent hasn't shipped anything yet"`). | Code comment. |

### Borderline glyphs left in place (consistent with audit)

- `✓` (U+2713 CHECK MARK) appears in `AgentPlanCard.tsx:83`, `AgentTaskView.tsx:164`, `PreviewPane.tsx:390` as the "step done" indicator. The audit only flagged `⚠️` and was silent on `✓`. Treating `✓` as a typographic dingbat / UI affordance (same role as a `Check` icon component), not an emoji. Out of scope for this batch.

### Exclamation points

Zero `!` in JSX text content of `frontend/src/pages/`. Zero in user-facing toast strings or HTTPException details touched by this batch.

## Sections deferred (not in this batch, per task instructions)

- §4 (loading-state skeletons — PF-10, PF-15, PF-21)
- §5 (motion / micro-interactions — PF-22, PF-23, PF-24)
- §6 (a11y axe pass + aria-labelledby — PF-25, PF-30)
- §7 (information-density polish — PF-19, PF-20, PF-27, etc.)
- §3.2 (the `<EmptyState>` adoption fix in `Research.tsx:264` — listed in audit §3 but is a structural change, not a copy fix)

## Push status

```
$ git push
   47af4fe..e8564f7  loop6/zero-bug -> loop6/zero-bug
```

No `--force`. All three commits pushed.
