# A48 post-CC23 re-audit

## Verdict
CC-16 and CC-17 hold. CC-18/19 browser+sandbox scrubs hold. CC-22 and CC-21 do **not** fully hold, and CC-23 is incomplete because `npm install` is not idempotent.

## Findings summary
- **3 findings total**
- **2 medium**
- **1 low**

## Verification results

### CC-16 — slowapi hard dependency
Pass.
- `mariana/api.py:67-76` now hard-imports `slowapi` and no longer wraps it in `try/except ImportError`.
- No `_NoopLimiter` class exists in live code; only historical mentions remain in prior audit notes/comments.
- `mariana/api.py:431-436` contains the three startup assertions (`_slowapi is not None`, `isinstance(limiter, Limiter)`, `_RATE_LIMIT_STORAGE_VALIDATED`).
- `requirements.txt:16` pins `slowapi==0.1.9`.

### CC-17 — SECURITY DEFINER search_path pinning
Pass.
- Repo grep shows all current `SECURITY DEFINER` migration hits include `SET search_path`.
- `tests/test_cc17_security_definer_search_path.py` exists and is comprehensive: it parses every SQL migration plus the CI baseline, finds every `CREATE ... FUNCTION ... SECURITY DEFINER` block, asserts a `SET search_path` clause is present, includes a parser smoke test, and sanity-checks the parser finds many functions.
- Focused pytest run passed: `2 passed`.
- Independent parser check found **62 SECURITY DEFINER functions in migrations, 0 missing search_path**.

### CC-18 / CC-19 — browser_server and sandbox_server scrub verification
Pass.
- Targeted greps for `detail=f"...{exc}` / path interpolation in `browser_server/app.py` and `sandbox_server/app.py` returned zero matches.

### CC-20 / CC-21 / CC-22 scrub verification
Mixed.
- `mariana/agent/api_routes.py`: targeted grep for `task_id` in detail strings returned zero matches.
- `mariana/api.py` canonical CC-22 sites at the originally flagged helpers now use generic strings (`task not found`, `not found`).
- However, additional raw `task_id` 404 strings still exist elsewhere in `mariana/api.py` (Finding 1).
- `mariana/agent/loop.py` still persists and emits raw `ToolError` strings/details on one important path (Finding 2).

### CC-23 — dependency pinning, lockfile idempotence, B-44
Mixed.
- `frontend/package.json` and `e2e/package.json` have **zero** `^` / `~` entries in dependencies/devDependencies.
- `frontend/src/test/b44_jsdom_version.test.ts` exists and the focused Vitest run passed (`6 passed`).
- But `npm install` is **not idempotent**: both `frontend/package-lock.json` and `e2e/package-lock.json` changed on install (Finding 3).

### Migrations top-out check
Pass.
- `frontend/supabase/migrations/` currently has 45 SQL files and the maximum numeric prefix is **024**. No `025+` drift was found.

### Paranoid frontend forbidden-word round
No new actionable copy issue found in a quick pass.
- Grep still finds lexical matches, but the sampled hits were route names, code identifiers/comments, or false-positive substrings like `partnerships`, not clear remaining hero-copy violations.

## Findings

### Finding 1 — residual task-id disclosure in multiple 404s (CC-22 incomplete)
**Severity:** Medium

`mariana/api.py` still contains multiple user-facing 404 responses that interpolate the raw task identifier:
- `1551`
- `3366`
- `3412`
- `3438`
- `3480`
- `4385`
- `4466`
- `4560`
- `4621`
- `4800`

Representative pattern:
```py
raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
```

Impact:
- Reintroduces the same low-grade identifier disclosure/fingerprinting class CC-22 was meant to remove.
- Creates inconsistent wire contracts: some task 404s are generic, others still echo caller-supplied IDs.

Recommended fix:
- Replace all remaining `f"Task {task_id!r} not found"` sites with a stable generic detail such as `"task not found"`.
- Log the original `task_id` server-side before raising, as done in the canonical CC-22 fixes.

### Finding 2 — raw ToolError message/detail still reaches agent step error fields (CC-21 incomplete)
**Severity:** Medium

The `ToolError` path in `mariana/agent/loop.py` still persists raw exception text and detail into user-visible step state/event payloads:
- `mariana/agent/loop.py:942-950`
  - `step.error = str(exc)`
  - `step.result = {"error_detail": exc.detail}`
  - emitted payload includes `{"error": step.error, "detail": exc.detail}`

This is still dangerous because `ToolError` carries arbitrary message/detail values:
- `mariana/agent/dispatcher.py:31-34` stores a free-form `message` plus arbitrary `detail`.
- `mariana/agent/dispatcher.py:102-105` wraps browser/sandbox errors with `detail={"status": ..., "body": ...}`.
- Multiple dispatcher call sites also build path/file-listing-rich messages such as:
  - `source_dir {src_dir!r} is empty or missing`
  - `entry {entry!r} not found in {src_dir!r}. Available: ...`
  - `failed to read {sb_path}: {exc}`

Impact:
- A failed tool step can still expose raw workspace paths, filenames, remote response bodies, and other internal diagnostics through persisted step state / emitted events.
- This is the same class CC-21 intended to eliminate for loop error fields.

Recommended fix:
- Mirror the unexpected-exception path: persist only stable error codes on `step.error` / `step.result` / emitted payloads.
- Keep raw text/details only in structured server logs.
- Consider normalizing `ToolError` to carry an internal code and a safe public code separately.

### Finding 3 — `npm install` rewrites both lockfiles (CC-23 incomplete)
**Severity:** Low

`npm install --no-audit --no-fund` changed both lockfiles.

Observed diffs:
- `frontend/package-lock.json` rewrote root dependency spec entries from caret ranges to exact pins and updated stale root entries such as `react-router-dom`, `@testing-library/*`, `vite`, and `jsdom` to match current `package.json`.
- `e2e/package-lock.json` rewrote the root `playwright` spec from `^1.59.1` to `1.59.1`.

Impact:
- The repo does not satisfy the stated idempotence requirement yet.
- Fresh installs normalize lock metadata, which is supply-chain noise and a CI drift footgun even if resolved package versions stay materially the same.

Recommended fix:
- Commit regenerated `frontend/package-lock.json` and `e2e/package-lock.json` after the package manifest pinning changes.
- Re-run the idempotence check after commit.

## Additional security sweep notes

### Auth / permission checks
Quick spot-check looks generally sound.
- Most stateful/user data endpoints depend on `_get_current_user`, `_require_investigation_owner`, or `_require_admin`.
- SSE log streaming requires either a short-lived stream token or bearer auth, and stream-token minting itself requires ownership.
- Preview cookie is scoped, `HttpOnly`, `Secure`, and `SameSite=Lax`.

### CSRF
No new CSRF finding from this sweep.
- The main authenticated API relies on `Authorization: Bearer ...`, not ambient session cookies, so classic browser CSRF is not the primary risk shape here.
- The only app-set cookie I found is the preview cookie on the preview path.

### localStorage / dangerous HTML / websocket-SSE auth
No new finding from this sweep.
- No `localStorage` hits in `frontend/src/`; Supabase auth uses `sessionStorage` in `frontend/src/lib/supabase.ts`.
- No `dangerouslySetInnerHTML` hits in `frontend/src/`.
- No unauthenticated websocket endpoint found; SSE log streaming is authenticated.

### Prompt-injection hardening
No concrete new exploit found in this pass, but there is also no obvious dedicated prompt-injection filtering layer on the simple `/api/chat/respond` path beyond normal prompting and structured-response parsing. I would treat that as an area to keep reviewing, not a confirmed issue from this round.

## One-line verdict
Two of the eight post-CC fixes still have residual gaps (CC-21 and CC-22), and CC-23 is not complete until lockfiles are regenerated and stable.

## Confidence
Medium-high.
- I directly re-checked the requested files/lines, ran focused grep sweeps, ran the CC-17 regression test, ran the B-44 regression test, and performed fresh `npm install` idempotence checks.
- I did not run the full pytest/vitest suites in this pass, and the broader security review was a targeted audit rather than a full codebase line-by-line review.
