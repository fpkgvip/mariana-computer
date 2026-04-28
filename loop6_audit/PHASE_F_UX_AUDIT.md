# Phase F — UX / Copy Discovery Audit

**Repo:** `/home/user/workspace/mariana`
**Branch:** `loop6/zero-bug`
**HEAD:** `16280c7eb881b48b2652ee0d1c08ddc3a6ceae8b`
**Scope:** `frontend/src/` (user-visible copy, states, motion, a11y) + `mariana/api.py` (HTTPException details surfaced to users)
**Mode:** Discovery only. No source files modified.

## Locked rules (recap, used as the bar)

- 5 demands: CODING / WORKS / STEVE JOBS / UX / HACKER PROOF
- Voice: **Confident. Quiet. Competent.**
- Forbidden hero verbs: `build`, `ship`, `supercharge`, `empower`, `unlock`, `transform`, `accelerate`, `revolutionize`, `reimagine`
- Forbidden adjectives: `magical`, `amazing`, `stunning`, `seamless`, `effortless`, `beautiful`, `powerful`, `smart`, `next-gen`, `cutting-edge`, `world-class`
- Forbidden anywhere in user copy: `scrape` / `scraping` / `crawl`
- No emojis, no exclamation points, no marketing fluff in user-facing copy
- Engineering terms in code identifiers, comments, and runtime log lines (`build`, `unlock`, `buildId`, `isBuilding`, "passing build", "build/test/deploy") are NOT violations — only user-visible copy is in scope

**Severity legend.**
- **S1** — user-visible primary surface (marketing pages, hero, prompt bar default, primary CTAs). Fix ASAP.
- **S2** — user-visible secondary surface (settings, dialogs, error toasts, admin tabs visible to operators). Fix this loop.
- **S3** — borderline / context-dependent (example prompts that quote a hypothetical user, gerund forms in CTAs). Discuss before fixing.

---

## Section 1 — Forbidden-word violations

**Total: 11 violations** (8 hard hits + 3 borderline). Zero forbidden adjectives. One emoji in a user-facing toast. No exclamation points in user copy.

### Hard violations (must fix)

| # | Severity | File:Line | Current copy | Forbidden token | Suggested rewrite |
|---|---|---|---|---|---|
| 1.1 | **S1** | `frontend/src/pages/Research.tsx:163` | `title: "Scrape, normalize, and chart public data"` | `Scrape` (hard-blocked anywhere in user copy) | `"Pull, normalize, and chart public data"` |
| 1.2 | **S1** | `frontend/src/pages/Chat.tsx:3265` | `Ask a question, build a tool, run an analysis, draft a document, or automate a workflow.` | `build` (hero verb, in empty-state hint on the primary work surface) | `"Ask a question, write a tool, run an analysis, draft a document, or automate a workflow."` |
| 1.3 | **S1** | `frontend/src/components/deft/PromptBar.tsx:130` | `placeholder = "What should Deft build?"` (default prop value) | `build` | `placeholder = "What should Deft do?"` — note: every real call site in the repo passes its own placeholder, so this is the fallback only. Still fix because tests/storybook/dev surfaces hit the default. |
| 1.4 | **S1** | `frontend/src/pages/Skills.tsx:87` | `"Build spreadsheets and models — forecasts, budgets, pricing calculators, scenario analyses — in Excel format."` | `Build` | `"Spreadsheets and models — forecasts, budgets, pricing calculators, scenario analyses — in Excel format."` |
| 1.5 | **S1** | `frontend/src/pages/Skills.tsx:95` | `"Build full web apps, internal tools, dashboards, and landing pages — React/TypeScript, deployed to a live URL."` | `Build` | `"Full web apps, internal tools, dashboards, and landing pages — React/TypeScript, deployed to a live URL."` |
| 1.6 | **S1** | `frontend/src/pages/Product.tsx:87` | `What Deft can build during a single task` | `build` | `"What Deft delivers in a single task"` |
| 1.7 | **S1** | `frontend/src/pages/Product.tsx:285` | `"Build landing pages, campaigns, and tracking plans"` | `Build` | `"Landing pages, campaigns, and tracking plans"` |
| 1.8 | **S2** | `frontend/src/components/OnboardingWizard.tsx:241` | `case 3: return "Pick something to build";` (step heading) | `build` | `"Pick a first run"` |
| 1.9 | **S2** | `frontend/src/pages/Chat.tsx:1309` | `content: \`⚠️ ${warnMsg}\`` — prepends `⚠️` emoji to a user-visible warning message | emoji in user copy | Drop the emoji. Use the warning surface styling from `components/deft/states/ErrorState.tsx` (icon + tone) instead of inline glyph. |

### Borderline (discuss; defensible if we trust user-quoted form)

| # | Severity | File:Line | Note |
|---|---|---|---|
| 1.10 | **S3** | `frontend/src/components/OnboardingWizard.tsx:449` | CTA text `"Start building"`. Gerund of a forbidden verb. Even in CTAs we should hold the line. Suggest `"Start the run"`. |
| 1.11 | **S3** | `frontend/src/components/OnboardingWizard.tsx:249` | `"We'll get you to your first build in under a minute."` Noun form of a forbidden hero verb on a primary onboarding screen. Suggest `"You'll have a first run inside a minute."` |

### User-quoted example prompts (NOT counted as violations)

These are example prompts a hypothetical user would type. They live in arrays whose purpose is to demonstrate Deft *receiving* the forbidden word. Defensible. Flagged for completeness.

- `frontend/src/pages/Index.tsx:21` — `"Build a habit tracker with a streak heatmap and Supabase auth."`
- `frontend/src/pages/Research.tsx:52` — `"Build an internal admin dashboard..."`
- `frontend/src/pages/Research.tsx:101` — `"Build a landing page..."`
- `frontend/src/pages/Research.tsx:121` — `"Build next month's board deck..."`
- `frontend/src/components/OnboardingWizard.tsx:69, 71` — example prompts.

**Recommendation:** keep the user-quoted examples. Real users will type these words; the homepage demo loses authenticity if we sanitize *their* voice. We hold the line on what *Deft* says, not on what users say to Deft.

### Confirmed NOT present

- Adjective list (`magical`, `amazing`, `stunning`, `seamless`, `effortless`, `beautiful`, `powerful`, `smart`, `next-gen`, `cutting-edge`, `world-class`) — zero hits in `frontend/src/`. Good.
- `crawl` — zero hits in user copy.
- Hero verbs `supercharge`, `empower`, `revolutionize`, `reimagine`, `accelerate`, `transform` — zero hits. Good.
- `ship` — zero hits in user copy. Good.
- Exclamation points in user copy — zero hits in `pages/` (regex over JSX text only).
- Engineering uses of `build` (`/build` route, `buildId`, `isBuilding`, "build/test/deploy" describing the agent's pipeline) — out of scope.

---

## Section 2 — Error message inventory (and rewrites)

Backend `mariana/api.py` raises `HTTPException(detail=...)` strings that are surfaced verbatim by `frontend/src/lib/errorToast.ts`. Many leak vendor/internals or fail the **Confident. Quiet. Competent.** voice (vague, passive-blame, or telling the user what *we* failed at instead of what *they* should do).

**Total rewrites recommended: 18.**

### Auth surface (highest impact — every signed-in user can hit these)

| # | Severity | File:Line | Current detail | Issue | Suggested rewrite |
|---|---|---|---|---|---|
| 2.1 | **S1** | `mariana/api.py:1250` | `"Authentication service not configured"` | leaks ops state to end users | `"Sign-in is temporarily unavailable. Try again shortly."` |
| 2.2 | **S1** | `mariana/api.py:1261` | `"Authentication service unavailable"` | same | same as 2.1 |
| 2.3 | **S1** | `mariana/api.py:1271` | `"Authentication service unavailable"` | duplicate site | same as 2.1 |
| 2.4 | **S1** | `mariana/api.py:1265` | `"Invalid token"` | terse, no remediation | `"Your session is invalid. Sign in again."` |
| 2.5 | **S1** | `mariana/api.py:1275` | `"Token missing user identifier"` | leaks JWT internals | `"Your session is malformed. Sign in again."` |
| 2.6 | **S1** | `mariana/api.py:1287` | `"Missing or invalid authorization header"` | jargon | `"Sign in to continue."` |
| 2.7 | **S1** | `mariana/api.py:1306` | `"Missing or invalid authorization header"` | duplicate | same as 2.6 |
| 2.8 | **S1** | `mariana/api.py:249-266` (4 sites) | `f"Internal error: {e}"` raised on signup/sign-in paths | leaks Python exception text to the login screen | `"Sign-in failed. Try again, or contact support if this keeps happening."` Log the original `{e}` server-side with a trace id, and surface the trace id only. |

### Conversation / message CRUD (touches the work surface)

| # | Severity | File:Line | Current detail | Suggested rewrite |
|---|---|---|---|---|
| 2.9 | **S2** | `mariana/api.py:2451` | `"Failed to create conversation"` | `"Could not start a new conversation. Try again."` |
| 2.10 | **S2** | `mariana/api.py:2482` | `"Failed to list conversations"` | `"Could not load your conversations. Try again."` |
| 2.11 | **S2** | `mariana/api.py:2529` | `"Failed to fetch conversation"` | `"Could not load this conversation. Try again."` |
| 2.12 | **S2** | `mariana/api.py:2633` | `"Failed to update conversation"` | `"Could not save changes to this conversation. Try again."` |
| 2.13 | **S2** | `mariana/api.py:2668` | `"Failed to delete conversation"` | `"Could not delete this conversation. Try again."` |
| 2.14 | **S2** | `mariana/api.py:2724` | `"Failed to save message"` | `"Could not save your message. Try again."` |

Pattern: replace `"Failed to <verb>"` (passive, blame-the-system) with `"Could not <verb>. <Action>."` Same number of words; fits the voice.

### Infra / startup

| # | Severity | File:Line | Current detail | Suggested rewrite |
|---|---|---|---|---|
| 2.15 | **S2** | `mariana/api.py:622` | `"Database unavailable"` | `"Our database is offline. Try again in a moment."` |
| 2.16 | **S2** | `mariana/api.py:629` | `"Configuration not loaded"` | `"The service is starting up. Try again in a few seconds."` |

### Payments (user-facing)

| # | Severity | File:Line | Current detail | Suggested rewrite |
|---|---|---|---|---|
| 2.17 | **S1** | `mariana/api.py:5605` and `:5834` | `"Payment service error. Please try again."` | `"Payments are temporarily unreachable. Try again in a moment."` |
| 2.18 | **S1** | `mariana/api.py:5616` | `"Stripe did not return a checkout URL"` | leaks vendor name to user. `"Could not start checkout. Try again."` |
| 2.19 | **S1** | `mariana/api.py:5840` | `"Stripe did not return a portal URL"` | `"Could not open the billing portal. Try again."` |
| 2.20 | **S2** | `mariana/api.py:3229` | `"Failed to submit investigation. Please try again."` | acceptable voice, but vague — `"Could not submit your report. Try again, or contact support if this keeps happening."` |

### Defensible as-is (good voice, do not change)

- `mariana/api.py:4421` — `"Report PDF file is not available. It may still be generating or was removed."` Specific, calm, actionable.
- `mariana/api.py:4845, 4991` — `f"File type {suffix!r} not supported. Allowed: ..."` Specific and remediation-rich.
- `mariana/api.py:8081` — `f"RPC {fn} failed: {body}"` is internal-only (audit/admin path). Keep but never surface to end users.

### Frontend toast text (smaller, but worth flagging)

| # | Severity | File:Line | Current copy | Suggested rewrite |
|---|---|---|---|---|
| 2.21 | **S2** | `frontend/src/components/deft/SecretsTable.tsx:53` and `:62` | `"Could not copy."` | `"Could not copy to clipboard."` (be specific about the channel) |
| 2.22 | **S2** | `frontend/src/components/deft/VaultSetupWizard.tsx:50` | `"Vault setup failed."` | `"Could not set up your vault. Try again."` |
| 2.23 | **S2** | `frontend/src/pages/Chat.tsx:1691` | `"Connection failed — please refresh"` | `"Lost connection. Reload to reconnect."` (drops "please", which signals weakness in this voice) |

---

## Section 3 — Empty states inventory

The codebase ships **excellent** empty-state primitives in `frontend/src/components/deft/states/EmptyState.tsx`. Adoption is the issue, not design.

### Strong (keep)

| Surface | File:Line | Notes |
|---|---|---|
| Tasks page (no tasks) | `frontend/src/pages/Tasks.tsx` (uses `<EmptyState>`) | Has a meaningful headline + CTA back to studio. |
| Skills page | `frontend/src/pages/Skills.tsx` | Uses `<EmptyState>` with "Create a skill" CTA. |
| ProjectsSidebar | `frontend/src/components/deft/ProjectsSidebar.tsx` | Dense, calm, well-worded. |
| AccountView "Recent activity" | `frontend/src/components/deft/account/AccountView.tsx` | "No activity yet" with a CTA into the studio. Good. |
| LiveCanvas idle | `frontend/src/components/deft/LiveCanvas.tsx` | Restrained, doesn't shout. |

### Weak (fix this loop)

| # | Severity | File:Line | Current | Issue | Suggested rewrite |
|---|---|---|---|---|---|
| 3.1 | **S2** | `frontend/src/pages/Research.tsx:264` | `"No examples in this category yet."` rendered as a bare `<p>` | doesn't use the `<EmptyState>` primitive — no icon, no CTA, looks like a 404 inside a working page | Use `<EmptyState>` with a headline and one of: "Pick another category" (resets the filter) or "Open the studio with a custom run". |
| 3.2 | **S2** | `frontend/src/pages/Chat.tsx` empty conversation list | (handled inline with "No conversations yet" text — verify) | Same pattern as 3.1: should use the primitive consistently across pages. | `<EmptyState headline="No conversations yet" description="Anything you ask Deft shows up here." cta={{ label: "Start a run", to: "/" }} />` |

### Vault truly-empty case

`frontend/src/pages/Vault.tsx` and `components/deft/VaultSetupWizard.tsx` handle the never-set-up case well — wizard, three clear steps, no fluff. No change.

---

## Section 4 — Loading states inventory

The codebase has skeleton primitives but uses them inconsistently. Pattern: full-page spinners are common on surfaces where a skeleton would match the unlocked layout.

### Strong (keep)

- `frontend/src/pages/Tasks.tsx:196` — inline pulse skeletons for the task list. Matches the final layout. No CLS jolt. Good.
- `frontend/src/components/deft/states/LoadingState.tsx` — well-designed primitive, multiple variants.

### Weak — replace with skeletons (S1/S2)

| # | Severity | File:Line | Current | Suggested |
|---|---|---|---|---|
| 4.1 | **S1** | `frontend/src/pages/Vault.tsx:83` | `"Loading vault…"` full-page centered spinner | Replace with a skeleton card matching the unlocked vault layout (header + list of secret rows). Vault is a primary surface and the spinner-then-content jolt is large. |
| 4.2 | **S1** | `frontend/src/pages/Chat.tsx:3253` | spinner-only when switching conversations | Replace with skeleton message rows (alternating user/agent bubble heights) so the layout doesn't reflow when messages arrive. |
| 4.3 | **S2** | `frontend/src/pages/Chat.tsx:3079` | `"Loading..."` (three ASCII dots) | Use a single ellipsis character: `"Loading…"`. Consistent with the rest of the app. |
| 4.4 | **S2** | `frontend/src/pages/Admin.tsx:97-98` | `"Loading…"` full-screen spinner | Admin-only, lower priority, but the admin shell would benefit from a skeleton sidebar + table. |
| 4.5 | **S2** | `frontend/src/pages/Admin.tsx:115` | `"Verifying admin access…"` full-screen spinner | Acceptable for a 200-300ms gate; if it ever exceeds 800ms add a calmer "Checking your permissions…" line. |
| 4.6 | **S3** | `frontend/src/pages/admin/tabs/UsersTab.tsx:188`, `TasksTab.tsx:109`, `FlagsTab.tsx:176`, `AuditTab.tsx:78` | full-section `<Loader2>` spinners | Operator-only surfaces. Skeleton table rows would be better but lower priority than user-facing surfaces. |

### Disabled / pending state coverage on async buttons

Audited: `Pricing.tsx` checkout CTA, `Skills.tsx` create-skill modal, `PreflightCard.tsx` Start, `Vault.tsx` unlock, `BuyCredits.tsx` top-up, `OnboardingWizard.tsx` next/back. **All correctly disable + show a spinner inside the button while in flight.** No regressions found here.

---

## Section 5 — Micro-interactions and motion

### Strong

- `pages/Index.tsx`, `pages/Pricing.tsx`, `pages/Login.tsx`, `pages/Signup.tsx` CTAs use `transition-all` with hover scale/shadow — calm and quick.
- `components/deft/PreflightCard.tsx` Start button uses `active:scale-[0.98]` — satisfying tactile feedback.
- `StudioHeader` and `ProjectsSidebar` transitions are clean. No jumpy layout-shift observed.
- Modal dismiss animations (`AlertDialog`, `Dialog` from shadcn) are tuned and not over-staged.

### Worth a second look

| # | Severity | File:Line | Note |
|---|---|---|---|
| 5.1 | **S3** | `frontend/src/pages/Product.tsx` (multiple `<ScrollReveal>` wrappers) | Every paragraph fades in on scroll. Stacked reveals on a marketing page can feel slow on long pages — consider revealing whole sections instead of every block, or kill staggering after the first viewport. |
| 5.2 | **S3** | `frontend/src/pages/Index.tsx:139` hero | `min-h-[100svh]` + cycling placeholder + animated chips. Each individually is fine; together the hero is busy. Pick one moving thing. (Recommend: keep cycling placeholder, make chips static.) |
| 5.3 | **S3** | `frontend/src/pages/Pricing.tsx` FAQ | Uses native `<details>`; chevron rotation is browser default. Tighten via CSS `[open] > summary svg { transform: rotate(180deg); transition: transform 150ms; }` to feel deliberate. |

No animation-on-error or animation-on-success was found that violates the calm voice. No autoplaying media. Good.

---

## Section 6 — Accessibility scan

No critical violations. Three minor items:

| # | Severity | File:Line | Issue | Note |
|---|---|---|---|---|
| 6.1 | **S3** | `frontend/src/components/FileViewer.tsx:501`, `pages/Chat.tsx:3770`, `pages/Skills.tsx:195` | `<div onClick={...}>` used for modal backdrops | Each is the click-outside-to-close handler. The actual close button + Escape-to-close are present. Modern shadcn pattern. Acceptable. |
| 6.2 | **S3** | `frontend/src/pages/Skills.tsx:195` (CreateSkillModal) | the modal wrapper has `role="dialog"` but no `aria-labelledby` pointing at the heading | Heading is `<h3>`. Add an id and reference it: `aria-labelledby="create-skill-title"`. |
| 6.3 | **S3** | `frontend/src/components/Navbar.tsx` mobile menu button | menu is keyboard-reachable and has `aria-expanded`. No issue found. |

### Confirmed clean

- No `<img>` without `alt` in `frontend/src/`.
- No `<input>` without an associated `<label>` in user-authored components (shadcn primitives that take `id` from the caller all flow through `<FormLabel htmlFor>` correctly).
- Vault loader has `role="status"` set for screen-reader announcement.
- Color contrast: spot-checked headline text and muted text against backgrounds (bg-zinc-950 / text-zinc-400) — within WCAG AA bounds for body sizes used (≥14px). Not exhaustive; recommend an axe pass on `/`, `/pricing`, `/product`, `/skills`, `/research`, `/build/:id`, `/vault`, `/account` before launch.

---

## Section 7 — Information density and Steve-Jobs polish

Pure quality calls. Each is a *suggestion*, not a defect.

| # | Severity | File:Line | Today | Suggested |
|---|---|---|---|---|
| 7.1 | **S2** | `frontend/src/pages/Skills.tsx` page heading | `"Skills"` (single word) | `"Skills"` is fine but the subhead is generic. Try: `"Reusable playbooks. Auto-detected, or pick your own."` |
| 7.2 | **S1** | `frontend/src/pages/BuyCredits.tsx` | The page tells the user "coming soon" / placeholder copy | A primary money surface should never say "coming soon". Either ship it this loop or redirect to `/account` with a single line: `"Top up from your account page."` |
| 7.3 | **S2** | `frontend/src/pages/Tasks.tsx` row meta line | model name · spend · timestamp separated by `·` | Visually crowded at narrow widths. Use a 3-column grid (model / spend / time) so each has a fixed slot and the row scans left-to-right at any width. |
| 7.4 | **S3** | `frontend/src/components/deft/account/AccountView.tsx` "Recent activity" | every row is rendered identically | Auto-collapse rows older than 30 days into a "Show older" disclosure. Keeps the visible density tight on accounts with months of history. |
| 7.5 | **S2** | `frontend/src/pages/Index.tsx` hero | Eyebrow + Headline + Sub + Prompt + Chip row + Trust strip = 6 stacked vertical zones | Consider folding the trust strip into the prompt-bar footer (logos as a single muted row directly under the input, not a separate section). One fewer scroll zone. |
| 7.6 | **S2** | `frontend/src/components/deft/PreflightCard.tsx` insufficient-credits message | links to `/checkout` | `/checkout` jumps users mid-funnel. Link to `/pricing` so they can see plans, then choose. Same friction, more context. |
| 7.7 | **S3** | `frontend/src/pages/Login.tsx` | `"Welcome back."` | Generic. Try: `"Pick up where Deft left off."` (still calm, says something). |
| 7.8 | **S3** | `frontend/src/components/OnboardingWizard.tsx:241` step 3 heading | `"Pick something to build"` | Already flagged in §1 as a forbidden-verb fix. The replacement `"Pick a first run"` is also stronger UX — "first run" implies repeated use, "build" implies a one-shot. |
| 7.9 | **S3** | `frontend/src/pages/Build.tsx` cancel-run dialog | `"Cancel this run?"` / `"Keep running"` / `"Cancel run"` | Already strong. No change. (Logged as a positive example for future copy reviews.) |
| 7.10 | **S3** | `frontend/src/pages/Pricing.tsx` headline | `"You only pay for software that runs."` | Already strong. No change. |
| 7.11 | **S3** | `frontend/src/components/deft/studio/StudioHeader.tsx` | timestamp + credits + status all in the header | Already well-densified. No change. |
| 7.12 | **S3** | `frontend/src/pages/Research.tsx` example cards | each card has runtime, prompt, output | Densification opportunity: surface the *output type* (Excel, dashboard URL, PPTX) as a chip so the user can filter visually. |

---

## Section 8 — Prioritised worklist

Ordered by **highest user-visible impact / lowest effort first**. Effort is rough engineering time including review.

| Item | Severity | File:Line | Effort | Description |
|---|---|---|---|---|
| **PF-01** | **S1** | `frontend/src/pages/Research.tsx:163` | 5 min | Replace `"Scrape, normalize, and chart public data"` with `"Pull, normalize, and chart public data"` (forbidden term `Scrape`). |
| **PF-02** | **S1** | `frontend/src/pages/Chat.tsx:3265` | 5 min | Replace `"Ask a question, build a tool, run an analysis, draft a document, or automate a workflow."` with `"Ask a question, write a tool, run an analysis, draft a document, or automate a workflow."` (forbidden hero verb `build` on the primary work surface empty state). |
| **PF-03** | **S1** | `frontend/src/components/deft/PromptBar.tsx:130` | 5 min | Change default placeholder from `"What should Deft build?"` to `"What should Deft do?"`. Real call sites override, but the default is what tests, storybook, and dev surfaces show. |
| **PF-04** | **S1** | `frontend/src/pages/Skills.tsx:87, :95` | 10 min | Drop `Build` from both card descriptions. Suggested rewrites in §1.4 / §1.5. |
| **PF-05** | **S1** | `frontend/src/pages/Product.tsx:87, :285` | 10 min | Drop `build` from `"What Deft can build during a single task"` and `"Build landing pages, campaigns, and tracking plans"`. Suggested rewrites in §1.6 / §1.7. |
| **PF-06** | **S1** | `frontend/src/pages/Chat.tsx:1309` | 5 min | Remove the `⚠️` emoji; rely on the existing `<ErrorState>` styling for tone. |
| **PF-07** | **S1** | `mariana/api.py:1250-1306` (8 sites) | 30 min | Rewrite all auth-failure detail strings per §2.1–§2.7. These hit every user, every session boundary. |
| **PF-08** | **S1** | `mariana/api.py:249-266` (4 sites) | 20 min | Replace `f"Internal error: {e}"` with `"Sign-in failed. Try again, or contact support if this keeps happening."` Log `{e}` server-side with a trace id and surface the trace id only. |
| **PF-09** | **S1** | `mariana/api.py:5605, 5616, 5834, 5840` | 20 min | Rewrite payment errors per §2.17–§2.19. Stop leaking the vendor name to end users. |
| **PF-10** | **S1** | `frontend/src/pages/Vault.tsx:83` | 30 min | Replace `"Loading vault…"` full-page spinner with a skeleton card matching the unlocked layout. Vault is a primary surface and the layout jolt is large. |
| **PF-11** | **S1** | `frontend/src/pages/BuyCredits.tsx` | 60 min | Either ship the page properly this loop, or redirect to `/account` with a single line `"Top up from your account page."` (do not leave "coming soon" copy on a money surface). |
| **PF-12** | **S2** | `frontend/src/components/OnboardingWizard.tsx:241, :249, :449` | 15 min | Drop `build` / `building` from headings + CTA. Suggested rewrites in §1.8 / §1.10 / §1.11. |
| **PF-13** | **S2** | `mariana/api.py:2451-2724` (6 sites) | 30 min | Rewrite `"Failed to <verb> conversation/message"` strings to `"Could not <verb>. Try again."` per §2.9–§2.14. |
| **PF-14** | **S2** | `mariana/api.py:622, :629` | 10 min | Rewrite infra startup errors per §2.15 / §2.16. |
| **PF-15** | **S2** | `frontend/src/pages/Chat.tsx:3253` | 30 min | Replace conversation-switch spinner with skeleton message rows. |
| **PF-16** | **S2** | `frontend/src/pages/Research.tsx:264` | 15 min | Replace bare `<p>` with `<EmptyState>` primitive + a "Pick another category" CTA. |
| **PF-17** | **S2** | `frontend/src/components/deft/SecretsTable.tsx:53, :62`; `components/deft/VaultSetupWizard.tsx:50`; `pages/Chat.tsx:1691` | 15 min | Rewrite frontend toast strings per §2.21–§2.23. |
| **PF-18** | **S2** | `frontend/src/components/deft/PreflightCard.tsx` insufficient-credits link | 5 min | Change link target from `/checkout` to `/pricing` so users see context before paying. |
| **PF-19** | **S2** | `frontend/src/pages/Tasks.tsx` row meta | 30 min | Convert `model · spend · time` row from inline separators to a 3-column grid for stable scanning. |
| **PF-20** | **S2** | `frontend/src/pages/Skills.tsx` subhead | 10 min | Add a confident subhead per §7.1. |
| **PF-21** | **S2** | `frontend/src/pages/Chat.tsx:3079` | 2 min | `"Loading..."` → `"Loading…"`. |
| **PF-22** | **S3** | `frontend/src/pages/Product.tsx` ScrollReveal stacking | 30 min | De-stagger reveals or reveal whole sections, not paragraphs (§5.1). |
| **PF-23** | **S3** | `frontend/src/pages/Index.tsx` hero motion | 20 min | Pick one moving element in the hero (§5.2). |
| **PF-24** | **S3** | `frontend/src/pages/Pricing.tsx` FAQ chevron | 10 min | Tighten chevron rotation timing (§5.3). |
| **PF-25** | **S3** | `frontend/src/pages/Skills.tsx:195` modal | 5 min | Add `aria-labelledby` referencing the heading id. |
| **PF-26** | **S3** | `frontend/src/components/deft/account/AccountView.tsx` | 45 min | Auto-collapse activity rows older than 30 days behind a disclosure (§7.4). |
| **PF-27** | **S3** | `frontend/src/pages/Login.tsx` welcome line | 5 min | Replace `"Welcome back."` per §7.7. |
| **PF-28** | **S3** | `frontend/src/pages/Index.tsx` hero zones | 60 min | Fold trust strip into prompt-bar footer (§7.5). |
| **PF-29** | **S3** | `frontend/src/pages/admin/tabs/*` | 60 min | Operator-only surfaces — replace `<Loader2>` spinners with skeleton table rows. Lowest priority. |
| **PF-30** | **S3** | full a11y axe pass | 90 min | Run axe DevTools on `/`, `/pricing`, `/product`, `/skills`, `/research`, `/build/:id`, `/vault`, `/account`. Capture findings as a follow-up issue. |

### Suggested grouping into work batches

- **Batch 1 (S1 copy, ~1 hr):** PF-01, PF-02, PF-03, PF-04, PF-05, PF-06, PF-12, PF-21.
- **Batch 2 (S1 backend errors, ~1.5 hr):** PF-07, PF-08, PF-09, PF-13, PF-14, PF-17.
- **Batch 3 (S1 surfaces, ~2 hr):** PF-10, PF-11, PF-15, PF-16, PF-18.
- **Batch 4 (S2 polish, ~1.5 hr):** PF-19, PF-20, PF-22, PF-23, PF-24.
- **Batch 5 (S3 polish, optional this loop):** PF-25 — PF-30.

Total S1+S2 work to take this surface to a **Confident. Quiet. Competent.** bar: roughly 7-8 hours of focused engineering + review.

---

## Method note

This audit ran without modifying any source. Tools used: `grep` (regex over `frontend/src/**/*.{ts,tsx}` and `mariana/api.py`), targeted `read` of every page in `frontend/src/pages/`, every component in `frontend/src/components/deft/`, the auth/conversation/payment surfaces of `mariana/api.py`, and the empty/error/loading-state primitives. File:line references were resolved against `HEAD = 16280c7eb881b48b2652ee0d1c08ddc3a6ceae8b` on `loop6/zero-bug`.
