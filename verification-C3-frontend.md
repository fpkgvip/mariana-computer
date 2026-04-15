# Verification C3: Frontend & Integration
Date: 2026-04-15
Auditor: Claude Sonnet (frontend specialist)

---

## Files Reviewed

| File | Lines |
|------|-------|
| `frontend/src/pages/Chat.tsx` | 2029 |
| `frontend/src/contexts/AuthContext.tsx` | 251 |
| `frontend/src/pages/Login.tsx` | 108 |
| `frontend/src/pages/Signup.tsx` | 109 |
| `frontend/src/pages/Admin.tsx` | 462 |
| `frontend/src/pages/Account.tsx` | 192 |
| `frontend/src/pages/BuyCredits.tsx` | 77 |
| `frontend/src/pages/Checkout.tsx` | 198 |
| `frontend/src/pages/Index.tsx` | 222 |
| `frontend/src/pages/Pricing.tsx` | 284 |
| `frontend/src/pages/Research.tsx` | 174 |
| `frontend/src/pages/Mariana.tsx` | 333 |
| `frontend/src/pages/Skills.tsx` | 391 |
| `frontend/src/pages/Contact.tsx` | 141 |
| `frontend/src/pages/ResetPassword.tsx` | 166 |
| `frontend/src/pages/NotFound.tsx` | 25 |
| `frontend/src/lib/supabase.ts` | 16 |
| `frontend/src/lib/utils.ts` | 7 |
| `frontend/src/App.tsx` | 64 |
| `frontend/src/main.tsx` | 6 |
| `frontend/src/components/Navbar.tsx` | 214 |
| `frontend/src/components/Footer.tsx` | 50 |
| `frontend/src/components/FileUpload.tsx` | 376 |
| `frontend/src/components/FileViewer.tsx` | 490 |
| `frontend/src/components/ProgressTimeline.tsx` | 499 |
| `frontend/src/components/ScrollReveal.tsx` | 42 |
| `frontend/src/components/NavLink.tsx` | 29 |
| `frontend/vite.config.ts` | 28 |
| `frontend/package.json` | 90 |
| `mariana/api.py` (key sections for cross-reference) | 3159 |

---

## Bugs Found

### BUG-C3-01: SSE `status_change` completion trigger uses wrong state name — investigation never terminates via SSE structured path

- **File**: `frontend/src/pages/Chat.tsx`, line 798
- **Severity**: major
- **Description**: The SSE `handleLogEvent` handler for structured events checks `parsed.state === "HALT"` to detect investigation completion. The backend (api.py line 1008) sets the DB status to `"HALTED"` (the `InvestigationStatus` enum value), but the SSE `state_change`/`status_change` event from the structured path carries state machine string values like `"COMPLETED"`, `"FAILED"`, `"HALTED"` — not the literal string `"HALT"`. The DB-fallback SSE path (line 1327) emits `state_change` events with `row["status"]` which is `"HALTED"` not `"HALT"`. The Redis structured-events path emits `status_change` events and a terminal `done` event (handled separately) — but if a `status_change` with `state: "HALT"` is somehow emitted, it would never match the frontend check because the backend never produces `state: "HALT"` in structured events either (it uses the full word `"HALTED"`). The result is that the `status_change` path in the structured-event handler never fires the completion handler. Completion is handled correctly via the separate `done` event listener (lines 878–916), so the investigation does complete eventually — but if the `done` event is missed/dropped (network), the chat stays stuck in "researching" with no completion notice. The `state_change` event handler (lines 919–933) also handles `COMPLETED`/`FAILED` status from the DB-fallback path, and those work. The structured-event `status_change` completion via `"HALT"` is simply dead code.
- **Trigger**: Investigation completes and backend emits a structured `status_change` event with any state other than `"HALT"` (which is always the case).
- **Impact**: The `if (eventType === "status_change" && parsed.state === "HALT")` branch is unreachable. Completion is still handled through the `done` event listener, so the practical impact is low in normal operation. However, if a structured event with `state: "HALTED"` were emitted, it would not trigger completion cleanup. The dead branch also suggests the wrong name was intended — `"HALTED"` — but since it's unreachable it doesn't break anything today.
- **Fix**: Either change line 798 to `parsed.state === "HALTED"` to match the backend's `InvestigationStatus` value, or remove the condition entirely and rely solely on the `done` event listener for completion. The latter is cleaner since terminal state is already handled by the dedicated `done` handler.

---

### BUG-C3-02: `Admin.tsx` auth guard uses strict `user === null` — flickers to login during Supabase token refresh

- **File**: `frontend/src/pages/Admin.tsx`, lines 98–106
- **Severity**: minor
- **Description**: The Admin page auth guard fires `navigate("/login", { replace: true })` immediately when `user === null`, with no grace period. Every other protected page in the app (`Chat.tsx` line 361, `Account.tsx` line 44, `BuyCredits.tsx` line 25, `Checkout.tsx` line 33, `Skills.tsx` line 264) uses a 500ms timeout to survive the brief `user=null` window during Supabase token refresh. Admin skips this pattern entirely.
- **Trigger**: Admin user navigates to `/admin` during a Supabase token refresh cycle (which happens automatically ~every hour).
- **Impact**: Admin is redirected to `/login`, losing their current view. They must re-navigate to `/admin` after the token refreshes.
- **Fix**: Apply the same pattern as other protected pages:
  ```tsx
  useEffect(() => {
    if (user === null) {
      const timer = setTimeout(() => {
        navigate("/login", { replace: true });
      }, 500);
      return () => clearTimeout(timer);
    }
    if (user.role !== "admin") {
      navigate("/chat", { replace: true });
    }
  }, [user, navigate]);
  ```

---

### BUG-C3-03: `FileUpload.tsx` — concurrent file upload race condition on `uploadedFilesRef` sync

- **File**: `frontend/src/components/FileUpload.tsx`, lines 130, 146–151
- **Severity**: minor
- **Description**: `uploadedFilesRef` is assigned `uploadedFiles` prop on every render (line 112: `uploadedFilesRef.current = uploadedFiles`). The `uploadFile` function reads `uploadedFilesRef.current` at the start to add the new file entry (line 130), then later inside the XHR progress callback reads it again (line 146) to update progress. Between the time the initial file entry is added via `onFilesChange([...uploadedFilesRef.current, newFile])` (line 130) and when the parent re-renders, `uploadedFilesRef.current` still holds the old value. If two files are uploaded in the same tick, both calls read the same stale snapshot and their `onFilesChange` calls overwrite each other's additions. The comment in the code (lines 105–112) claims this is fixed by using the ref, but the fix is incomplete: the ref is updated by React only on re-render, so between the `onFilesChange` call (which queues a state update) and the re-render (which syncs the ref), concurrent `uploadFile` invocations still see stale data.
- **Trigger**: User selects multiple files simultaneously (e.g., via Ctrl+click in the file picker) when `handleFiles` calls `uploadFile` in a `for` loop (line 222–228). All `uploadFile` async calls start before any re-render occurs.
- **Impact**: With 3 files selected, only the last file's entry survives in state — the first two are overwritten. The user sees fewer files than expected in the pre-send chip list.
- **Fix**: Use a functional state update with a callback instead of spreading `uploadedFilesRef.current`:
  ```tsx
  // Adding new file:
  onFilesChange((prev: UploadedFile[]) => [...prev, newFile]);
  
  // Updating progress:
  onFilesChange((prev: UploadedFile[]) => {
    const idx = prev.findIndex((f) => f.name === file.name && f.size === file.size);
    if (idx < 0) return prev;
    const updated = [...prev];
    updated[idx] = { ...updated[idx], progress: pct };
    return updated;
  });
  ```
  However, `onFilesChange` is typed as `(files: UploadedFile[]) => void` — it does not accept a function updater. The prop type would need to change to `(files: UploadedFile[] | ((prev: UploadedFile[]) => UploadedFile[])) => void`. An alternative is to track pending additions in a local ref that accumulates synchronously before the first re-render.

---

### BUG-C3-04: `switchInvestigation` includes `timelineSteps` in `useCallback` deps — causes stale closure / excessive recreation

- **File**: `frontend/src/pages/Chat.tsx`, line 1033
- **Severity**: minor
- **Description**: `switchInvestigation` includes `timelineSteps` in its dependency array (line 1033). `timelineSteps` is a state variable that changes on every SSE progress event during an active investigation. This causes `switchInvestigation` to be recreated on every timeline update, which in turn causes every investigation sidebar button (which calls `switchInvestigation`) to re-render unnecessarily. More importantly, the function body reads `timelineSteps` directly to save state (`timelineStoreRef.current[activeTaskId] = [...timelineSteps]` on line 988), which makes the captured `timelineSteps` the value at the time of the last `switchInvestigation` recreation, not at the moment of the call — so if a timeline step arrives between the recreation and the user clicking switch, that step is lost.
- **Trigger**: User switches investigations while an active one is still receiving timeline steps.
- **Impact**: The most-recent timeline steps accumulated since the last `switchInvestigation` recreation may not be saved. Minor UI inconsistency: when the user returns to the investigation, the last few steps are missing from the saved timeline.
- **Fix**: Use a ref for `timelineSteps` (similar to how `messagesRef` is used for `messages`) and remove `timelineSteps` from the dependency array:
  ```tsx
  const timelineStepsRef = useRef<TimelineStep[]>([]);
  timelineStepsRef.current = timelineSteps;
  
  // In switchInvestigation, replace `timelineSteps` with `timelineStepsRef.current`
  // and remove timelineSteps from the deps array.
  ```

---

### BUG-C3-05: `Pricing.tsx` — hardcoded plan IDs `"individual"` / `"enterprise"` may not match backend Supabase `plans` table IDs

- **File**: `frontend/src/pages/Pricing.tsx`, lines 23–54
- **Severity**: major
- **Description**: `Pricing.tsx` uses hardcoded plan objects with `id: "individual"` and `id: "enterprise"` (lines 23, 39). These IDs are sent to `POST /api/billing/create-checkout` as `plan_id`. The backend billing endpoint looks up the plan by ID from the Supabase `plans` table. `Checkout.tsx` correctly fetches plans from Supabase and uses real `plan.id` values. However, `Pricing.tsx` uses the hardcoded slugs. If the Supabase `plans` table uses UUIDs or different slugs as primary keys, the checkout API call will fail with a 404/400 error — the user clicks "Get Started", sees a spinner briefly, then gets a toast error.
- **Trigger**: User clicks "Get Started" on the Pricing page.
- **Impact**: Checkout flow broken from the Pricing page. The Checkout page (/checkout) works correctly because it fetches plans from Supabase, but the Pricing page does not.
- **Fix**: Either fetch plans from Supabase on the Pricing page (as `Checkout.tsx` does) and use real IDs, or ensure the Supabase `plans` table uses the slugs `"individual"` and `"enterprise"` as primary keys. The latter is a backend concern — verify the DB schema. Given that `Checkout.tsx` explicitly fetches plans, the mismatch risk is real and `Pricing.tsx` should mirror that approach.

---

### BUG-C3-06: `handleSend` missing `startInvestigation` in `useCallback` dependency array — stale closure risk

- **File**: `frontend/src/pages/Chat.tsx`, lines 1131–1137
- **Severity**: minor
- **Description**: The comment at lines 1131–1136 documents that `startInvestigation` is intentionally omitted from `handleSend`'s `useCallback` deps because of temporal ordering (it's defined after `handleSend`). The comment claims this is acceptable because "when handleSend is *called*, startInvestigation will have been assigned in the same render cycle." This reasoning is partially correct: both functions are defined in the same render, so the closure captures the current render's version of `startInvestigation`. However, if `handleSend`'s own deps (e.g., `isSending`, `input`) don't change between renders but `startInvestigation`'s deps do change (e.g., `uploadSessionUuid` changes), `handleSend` will hold an outdated `startInvestigation` that was captured in a previous render. The deps that `startInvestigation` closes over (`user`, `uploadSessionUuid`, `appendMessage`, `startTimer`, `startSSE`, `navigate`) could drift relative to `handleSend`'s stale closure.
- **Trigger**: User changes `uploadSessionUuid` (uploads a file), then sends a message. `handleSend` may call the previous render's `startInvestigation` which closed over `uploadSessionUuid = null`.
- **Impact**: File upload session UUID not passed to the investigation API, so uploaded files are not attached to the investigation. User sees their files in the chip list but they don't arrive in the backend investigation context.
- **Fix**: The correct solution is to use a ref to hold `startInvestigation` so `handleSend` can always call the latest version without needing it in deps:
  ```tsx
  const startInvestigationRef = useRef(startInvestigation);
  startInvestigationRef.current = startInvestigation;
  
  // In handleSend, call startInvestigationRef.current(...) instead of startInvestigation(...)
  // Remove the comment workaround.
  ```

---

## Verified Correct

**Auth Flow**
- `AuthContext.tsx` correctly relies on `onAuthStateChange(INITIAL_SESSION)` for initial session load — no double fetch.
- `login()` delegates entirely to `onAuthStateChange` — no double `fetchProfile`/`setUser` race.
- `signup()` correctly handles email-confirmation-disabled case with retry loop for profile trigger.
- `logout()` clears local state even on Supabase `signOut` failure.
- `refreshUser()` correctly calls `syncSession` with the current session.
- Token refresh is handled transparently by Supabase JS SDK — `getSession()` auto-refreshes; all API calls use `getAccessToken()` which calls `getSession()` fresh each time.
- Auth loading spinner prevents any content flash before session resolves.
- Protected route grace period (500ms timeout) correctly handles the brief `user=null` during token refresh in `Chat.tsx`, `Account.tsx`, `BuyCredits.tsx`, `Checkout.tsx`, `Skills.tsx`.

**Chat Interface**
- SSE connection uses `?token=` query parameter correctly — native `EventSource` cannot send headers, this is the standard workaround.
- SSE fallback to polling is triggered on `onerror` with `hasFailedOver` flag preventing multiple polling loops (BUG-002 fix verified).
- Polling gets a fresh token on every tick (BUG-R1-02 fix verified, line 660).
- `seenStatusIds` correctly implements a sliding-window trim to prevent unbounded Set growth (lines 526–529).
- `messagesRef.current = messages` pattern (line 352) — valid React pattern for synchronizing a ref during render to avoid stale closures.
- SSE named event listeners correctly registered: `log`, `done`, `state_change`, `error`, `ping` (BUG-R2-08 / BUG-R5-01 fixes verified).
- Investigation lifecycle correctly: classify → (plan approval for standard/deep) → start → SSE → done → completion message → download buttons.
- `handleDownload` correctly uses authenticated fetch + blob URL + DOM anchor click with async `URL.revokeObjectURL` (BUG-007, BUG-R2-15, BUG-R1-19 fixes verified).
- Credits update via `refreshUser()` at investigation completion (BUG-018 fix verified).
- Credit animation deduplication via `prevTokensRef` (BUG-R2-S2-07 fix verified).
- Message deduplication for status messages using `seenStatusIds` (BUG-019 fix verified).
- Zero/low-credit banners shown correctly at `user.tokens <= 0` and `< 1000`.
- 402 (insufficient credits) response correctly shows error with estimated cost and current balance (lines 1187–1205).
- 429 (rate limit) response correctly reads `Retry-After` header and shows message.
- File upload state cleared after investigation starts (line 1272).
- Per-investigation message store (`messageStoreRef`) correctly saves/restores messages on investigation switch.

**API Integration**
- All authenticated API calls include `Authorization: Bearer ${token}` header.
- `POST /api/investigations/classify` — request body `{ topic }` matches `ClassifyRequest` model.
- `ClassifyResponse` type in frontend (`tier`, `plan_summary`, `estimated_duration_hours`, `estimated_credits`, `requires_approval`) matches backend `ClassifyResponse` model exactly.
- `POST /api/investigations` — request body `{ topic, plan_approved, upload_session_uuid? }` matches `StartInvestigationRequest`.
- `StartInvestigationResponse` fields (`task_id`, `status`, `message`) match backend model.
- `GET /api/investigations/{task_id}` polling — frontend correctly uses `data.id` and `data.current_state` matching `TaskSummary` model (BUG-R2-02 fix verified).
- `GET /api/investigations/{task_id}/logs?token=...` SSE endpoint — matches backend `@app.get("/api/investigations/{task_id}/logs")`.
- `GET /api/investigations/{task_id}/report` PDF download endpoint matches backend.
- `GET /api/investigations/{task_id}/report/docx` DOCX download endpoint — needs backend verification but path pattern is consistent with PDF.
- `POST /api/upload` for pre-investigation file uploads matches backend `@app.post("/api/upload")` (BUG-R3-01 fix verified).
- File upload response `{ session_uuid }` correctly read (line 182).
- `GET /api/memory` response shape `{ facts, preferences }` matches backend `MemoryResponse`.
- `DELETE /api/memory/facts` with `{ fact }` body matches backend.
- `DELETE /api/memory/preferences` with `{ key }` body matches backend.
- `GET /api/skills` correctly filters out built-in skills on frontend.
- `POST /api/skills` body `{ name, description, system_prompt, trigger_keywords }` matches backend.
- `POST /api/billing/create-checkout` body `{ plan_id, success_url, cancel_url }` — both `Pricing.tsx` and `Checkout.tsx` send correct fields.
- `Checkout.tsx` correctly reads `data.checkout_url` (BUG-R2-S2-01 fix verified).
- `GET /api/billing/portal` response `{ portal_url }` correctly used in `Account.tsx`.
- Admin endpoints: `GET /api/admin/stats`, `GET /api/admin/users`, `GET /api/admin/investigations`, `POST /api/admin/users/{id}/credits` all use `Authorization` header correctly.
- Admin `/api/admin/users` returns a plain array (not paginated), and frontend correctly handles both array and `data.items` formats (lines 139–140).
- Admin `/api/admin/investigations` returns paginated `{ items, total, page, page_size }`, frontend correctly extracts `.items` (lines 160–161).
- `POST /api/admin/users/{id}/credits` body `{ credits: amount, delta: true }` matches `AdminSetCreditsRequest`.

**React Correctness**
- `useEffect` cleanup functions present for: timer intervals (lines 483–487), SSE EventSource (line 485), `onAuthStateChange` subscription (lines 129–132), scroll observer (ScrollReveal.tsx line 28), Navbar event listeners (lines 25, 36), ResetPassword timeout (line 55).
- `AuthImage` and `AuthVideo` components correctly revoke blob URLs in cleanup and handle cancellation with the `cancelled` flag.
- `FileContent` (FileViewer.tsx) correctly fixes the blob URL revocation closure bug — uses `localBlobUrl` variable captured in the effect (BUG-R2-S2-08 fix verified).
- `ProgressTimeline` correctly uses `useMemo` for grouping (BUG-R2-S2-06 fix verified).
- All list renders use stable keys: `investigations.map` uses `inv.task_id`, `messages.map` uses `msg._id` (always set), `plans.map` uses `plan.id`, `users.map` uses `u.user_id`, etc.
- `TimelineStepRow` timer interval correctly cleaned up in `useEffect` return (lines 257–260).
- Drag counter ref in `FileUpload` prevents false `isDragging` false-positive on nested element leave.

**TypeScript**
- `supabase.ts` correctly avoids `as string` casts — uses proper `string | undefined` with runtime guards (BUG-028 fix verified).
- `InvestigationPollResponse` type in Chat.tsx correctly documents `id` and `current_state` fields matching backend `TaskSummary`.
- `renderMarkdown` uses bounded quantifiers (`{1,200}`, `{1,500}`) to prevent ReDoS. Link regex restricted to `https?://` to prevent XSS.
- All `catch` clauses in async handlers are present and show errors via `toast.error`.

**UX**
- Zero-credits state disables the input and send button, shows upgrade banner and link to /pricing.
- Low-credits (<1000) warning shown with link to /pricing.
- Loading indicators present for: classifying (Loader2 spinner), sending/investigating (Loader2 spinner), credit portal opening, admin data fetching, file uploading (progress bar + spinner), skills loading.
- Retry button shown on error messages when `retryPayload` is set.
- `ResetPassword.tsx` correctly uses `isReadyRef` to prevent race between timeout and PASSWORD_RECOVERY event (BUG-R2-S2-05 fix verified). `isError && !isReady` guard on the error banner prevents simultaneous display of form and error.
- Password reset email redirects to `${window.location.origin}/reset-password` which is a registered route in `App.tsx`.
- `/buy-credits` route correctly redirects to `Checkout` component for backward compatibility.
- Navigation: all internal links verified against `App.tsx` routes — no dead links.
- Mobile sidebar: correctly uses overlay backdrop, translate-x animation, and closes on outside click.
- Navbar user menu: closes on outside click (mousedown handler), closes on route change, Escape key closes it.
- File viewer slide-over: correct backdrop, download with auth header, blob URL cleanup.
- Memory panel: correctly loads on open, shows loading spinner, allows deleting facts and preferences.

**Security**
- All `dangerouslySetInnerHTML` usage (Chat.tsx line 1741, FileViewer.tsx line 259) escapes HTML before markdown processing — XSS safe.
- Supabase investigation load filters by `user_id` (Chat.tsx line 456) — defense in depth on top of RLS.
- Admin route guard in `Admin.tsx` checks both `user === null` and `user.role !== "admin"` (lines 98–105) — non-admins are redirected to `/chat`.
- FileUpload validates file extension and size before upload (lines 78–86).
- File download uses auth header, not direct URL — prevents unauthenticated access.

**Build / Config**
- `vite.config.ts` disables source maps in production, deduplicates React packages, correct `@` alias.
- `package.json` versions are current and consistent.
- Environment variable guards in `supabase.ts` throw at startup if missing — fail-fast behavior.
- `API_URL = import.meta.env.VITE_API_URL ?? ""` pattern in multiple files — correct fallback to same-origin for API calls when no URL configured.
