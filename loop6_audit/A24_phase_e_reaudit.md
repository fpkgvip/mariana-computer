# Phase E re-audit #19

Header: model=claude_opus_4_7, commit=6c3978c, scope=broader sweep covering reservation, non-agent ledger paths, concurrency, vault/RLS, SSE auth, frontend XSS, schema posture, file uploads, Stripe webhook, with T-01 spot-check.

## Surface walkthrough

### Probe 1 — Reservation/admission billing flow
- `POST /api/agent` reservation logic at `mariana/agent/api_routes.py:439-511`. Reservation amount is `max(100, int(body.budget_usd * 100))` (100 credits/USD floor) — independent of model pricing. Settlement happens later via `_settle_agent_credits`, which uses actual `task.spent_usd * 100`.
- Reservation released paths confirmed:
  - Insert failure → `_supabase_add_credits` refund at `api_routes.py:492-510`.
  - Terminal state (DONE / FAILED / HALTED / CANCELLED) → `_settle_agent_credits` in worker `finally` block uses idempotent `grant_credits` for refund-of-unused or `refund_credits` for overrun (`loop.py:683-739`).
  - Stop endpoint finalizes pre-execution cancel + settle (O-02 fix referenced at `main.py:758-768`).
- Re-reservation on retry: not possible — the `_insert_agent_task` row is the unique identifier. A retried POST creates a new task row with a new task_id and a fresh reservation (charged again, as expected).
- Enqueue failure (`api_routes.py:533-537`) is caught and only logged. The row is in DB with reserved credits already deducted; the queue daemon's stale-task recovery (`main.py:752-778`) re-pushes any `agent_tasks` row in non-terminal state with `updated_at < NOW() - 60s`, so the task does eventually run and settle. Not a leak.
- Conclusion: no fresh bug.

### Probe 2 — Non-agent ledger paths
- `grep` for `spend_credits` / `grant_credits` / `refund_credits` in `mariana/*.py` (excluding agent + billing module): zero callsites for `spend_credits` anywhere in app code, and `grant_credits` / `refund_credits` are called only in `mariana/api.py` (Stripe handlers via `_grant_credits_for_event` and `_reverse_credits_for_charge`) and `mariana/agent/loop.py` (settlement). Conversation rename / auto-title / chat endpoints do not deduct credits — `chat_respond` explicitly states "No credits consumed for conversational replies" (`api.py:2006-2007`).
- Research/investigation submit (`api.py:2885-2918`) uses the legacy `_supabase_deduct_credits` → `deduct_credits` RPC with `(target_user_id, amount)` — no `ref_id`, so the spend is not idempotent on a per-request key. This is the historical B-05 design (legacy `deduct_credits` updates `profiles.tokens` directly without going through `credit_buckets`/`credit_transactions`); B-05 is a registered P1 in REGISTRY.md and not a new finding here.
- `_supabase_deduct_credits` returns `"ok" / "insufficient" / "error"`; the route refunds the reservation on every error path that aborts after deduction (e.g. session-conflict refund at `api.py:2942-2955`, plus the broader `try/except` wrapping the rest of the submit flow that I previously walked in re-audit #16).
- Stripe `grant_credits_for_event` (`api.py:6084-6150`) keys on `ref_type='stripe_event'` + `ref_id=event_id`, and the live function is duplicate-suppressed (re-audit #18 confirmed). Webhook events also have a separate two-phase claim (`_claim_webhook_event`).
- Conclusion: no fresh bug. Only known-and-registered B-05 surface in this area.

### Probe 3 — Concurrency / queue starvation
- Global concurrency cap is `_AGENT_MAX_CONCURRENT = int(os.getenv("AGENT_MAX_CONCURRENT", "4"))` (`main.py:735`). There is **no per-user concurrency limit** in either `POST /api/agent` or the queue daemon.
- The daemon uses `redis.blpop("agent:queue", timeout=5)` (`main.py:808`) — strict FIFO, no per-user round-robin. A malicious user with sufficient credits could enqueue many tasks back-to-back and starve the global pool. Each task still costs ≥100 credits per submission, so an attacker pays per slot consumed. This is a known architectural design (no SLA promised here), not an exploit and not a regression.
- Poison-pill: if `_load_agent_task` raises or returns `None`, `_run_one` catches the exception, logs, and returns (`main.py:780-793`); the task is removed from the queue and the daemon continues. Malformed payload does not block the worker pool.
- Conclusion: no fresh bug; observation-only on global concurrency.

### Probe 4 — Vault / secrets / RLS
- `mariana/vault/runtime.py:203-222` — `redact_payload` only walks `str`, `dict`, `list`, `tuple`. `bytes` and `bytearray` fall through unchanged (returned at line 222). However, the only caller wrapping tool / event data is `_record_event` (`loop.py:233-251`), which first does `event.model_dump(mode="json")`; Pydantic's JSON mode coerces `bytes` to base64 strings, so the bytes-bypass is not reachable from the active call path. No exploit.
- LIVE RLS posture (`SELECT schemaname, tablename, rowsecurity FROM pg_tables WHERE schemaname='public'`): all 21 public tables have `rowsecurity=true`. No `false` rows.
- LIVE policies with `qual='true' OR qual IS NULL`: only two `qual='true'` rows — `plans.Plans are readable by everyone` (SELECT, intentional public catalog) and `system_status.system_status_read` (SELECT, intentional public health endpoint, columns are `frozen / frozen_reason / frozen_by uuid / frozen_at / maintenance_message / updated_at` — no PII beyond an admin uuid that is also exposed in audit metadata). The remaining `qual IS NULL` rows are INSERT policies whose `with_check` clauses I verified all enforce `auth.uid() = user_id` (or equivalent owner-of-parent join) — see Probe 4 RLS results in conversation. No `USING (true)` on a sensitive table.
- Conclusion: no fresh bug.

### Probe 5 — SSE / WebSocket auth
- `_mint_stream_token` / `_verify_stream_token` (`api.py:1378-1457`) use `hmac.new(secret, payload, sha256)` and `hmac.compare_digest` for constant-time verification. Payload `{user_id}|{task_id}|{exp}` binds the token to a specific task, so it cannot be replayed against a different task_id (line 1447 `if tok_task_id != task_id: raise`).
- `_mint_preview_token` uses a `preview|` scope prefix and `_verify_preview_token` enforces `scope == "preview"` (line 1415), so a stream token cannot be replayed against the preview route or vice versa.
- The SSE endpoint's auth dependency `_authenticate_stream_token_or_header` (`api.py:1497-1515`) accepts the stream token via query param OR an Authorization header bearing a real JWT — but only for header-based access (typical XHR/fetch). Browsers submitting EventSource cannot send custom headers, so the practical SSE flow uses the short-lived token. Raw JWTs are not embedded in URLs by the frontend (see `frontend/src/lib/streamAuth.ts` minted via `POST /api/investigations/{task_id}/stream-token`).
- Conclusion: no fresh bug.

### Probe 6 — Frontend XSS / CSRF
- `frontend/src/pages/Chat.tsx:298-353` (`renderMarkdownImpl`) HTML-escapes `& < >` first (line 300-303), then applies bounded markdown regexes; the link rule (line 336) restricts hrefs to `^https?://` and re-escapes quotes/backticks in the substituted text and URL.
- `frontend/src/components/FileViewer.tsx:90-160` (`renderMarkdownContent`) HTML-escapes `& < > " '` (line 92-97) before any markdown transform; link rule (line 128-149) checks `/^https?:\/\//i.test(href)` and otherwise renders link text only.
- iframe sandboxing: HTML preview iframe uses `sandbox=""` (most restrictive, `FileViewer.tsx:374`); PDF preview uses `sandbox="allow-same-origin"` only (line 394) for blob-URL same-origin reads, no `allow-scripts`. Adequate.
- CSRF: state-changing endpoints are token-bound — auth is via `Authorization: Bearer <jwt>` header (Supabase access token, see `_get_current_user`), which is not auto-attached by the browser, so traditional cookie-CSRF does not apply.
- Conclusion: no fresh bug.

### Probe 7 — Database migration / schema posture (LIVE)
- `pg_proc` query: 27 `prosecdef=true` functions in `public`. Every one has `proconfig` containing `search_path=…`. The set is split between `search_path=public, pg_temp` (most), `search_path=public, auth` (admin functions that resolve `auth.uid()`-style calls), and one outlier `admin_set_credits` with `search_path=""`.
- `admin_set_credits` body (fetched via `pg_get_functiondef`) uses fully schema-qualified references (`public.profiles`, `public.credit_buckets`, `public.credit_transactions`, `public.is_admin`, `public.admin_audit_insert`); `auth.uid()` is already qualified. Empty `search_path` is the most paranoid B-02 posture, not a vulnerability.
- Conclusion: no fresh bug.

### Probe 8 — File upload / sandbox
- Upload endpoints `POST /api/investigations/{task_id}/upload` (`api.py:4741-4868`) and `POST /api/upload` (`api.py:4871-…`) enforce: 10 MB per-file cap, max 5 files per investigation, ext allowlist (`_UPLOAD_ALLOWED_EXTENSIONS`), filename sanitization via `os.path.basename(re.sub(r"[^\w\-.]", "_", filename))`, dotfile / `.` / `..` rejection, post-write resolved-path containment check, and post-write symlink check (`dest.is_symlink()` → unlink + 400). Each file is streamed and aborted on cap.
- Per-user storage quota: not enforced beyond `_UPLOAD_MAX_FILES_PER_INVESTIGATION = 5`. Not a security issue per se given the credit-gated submission flow.
- Conclusion: no fresh bug.

### Probe 9 — Stripe webhook signature verification
- `stripe_webhook` (`api.py:5584-5673`) verifies the signature against PRIMARY then PREVIOUS secret via `_stripe.Webhook.construct_event` BEFORE any business logic; on failure both secrets, raises 400. Only after verification does it call `_claim_webhook_event` (two-phase: NEW / RETRY / DUPLICATE) and dispatch the handler.
- Replay protection: `_claim_webhook_event` returns `DUPLICATE` for already-completed events (`api.py:5669-5671`), and downstream `grant_credits` calls are independently idempotent on `ref_type='stripe_event'` + `ref_id=event_id` via `uq_credit_tx_idem` (live, confirmed re-audit #18). Same event_id replayed → no double credit.
- Conclusion: no fresh bug.

### Probe 10 — T-01 spot-check
- `grep -n "task.credits_settled = True" mariana/agent/loop.py mariana/agent/settlement_reconciler.py` returns 7 matches in `loop.py` and 0 in the reconciler. Each match in `loop.py` is preceded by either:
  - a Supabase-not-configured short-circuit (line 514 — deliberate escape hatch, no ledger to apply),
  - `existing_claim["completed_at"] is not None` (line 551 — already durable),
  - successful `_mark_settlement_completed(...)` (line 567),
  - successful `_mark_settlement_completed(...)` after a lost-race re-fetch (line 623),
  - successful `_mark_settlement_completed(...)` in the noop / Δ=0 branch (line 661),
  - both `_mark_ledger_applied` AND `_mark_settlement_completed` succeeding in the post-RPC happy path (line 817).
- Reconciler only writes `credits_settled = False` (`settlement_reconciler.py:185`) before retrying. No replay vector.
- Conclusion: T-01 fix preserved.

## Findings

## No findings
Probe 1: Reservation flow refunds on insert failure and settles via idempotent grant/refund_credits at terminal state; no silent re-reservation on retry.
Probe 2: No `spend_credits` callers in app code; only Stripe and agent settlement use grant/refund_credits and both key on idempotent ref_id; no per-call credit charge in chat / rename / auto-title.
Probe 3: Global concurrency cap with FIFO BLPOP; no per-user starvation invariant promised; poison-pill exception caught and queue continues.
Probe 4: All 21 public tables have RLS enabled; only `plans` and `system_status` use `qual='true'` and both are intentional public catalogs; INSERT policies have correct `with_check`; vault `redact_payload` bytes-bypass is not reachable through the JSON-mode model_dump call path.
Probe 5: SSE/preview tokens use HMAC-SHA256 with constant-time compare, scope prefix prevents cross-replay, task_id binding rejects token reuse for a different task.
Probe 6: HTML escape applied before markdown; link href allowlist `^https?://`; HTML iframe `sandbox=""`, PDF iframe `sandbox="allow-same-origin"` only; auth via Bearer header so cookie-CSRF non-applicable.
Probe 7: All 27 LIVE `SECURITY DEFINER` functions have `search_path` pinned in `proconfig`; `admin_set_credits` uses the most paranoid `search_path=""` with fully-qualified body references.
Probe 8: 10 MB per-file cap, 5 files per investigation, ext allowlist, filename sanitization, traversal containment check, post-write symlink rejection.
Probe 9: Stripe signature verified before any business logic; `_claim_webhook_event` two-phase idempotency plus `uq_credit_tx_idem` partial unique index prevent double-credit on replay.
Probe 10: Each `task.credits_settled = True` assignment is preceded by a durable marker write (or is the no-Supabase escape hatch); reconciler never sets True.

RE-AUDIT #19 COMPLETE findings=0 file=loop6_audit/A24_phase_e_reaudit.md
