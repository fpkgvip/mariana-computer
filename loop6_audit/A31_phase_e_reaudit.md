# A31 — Phase E Re-audit #26 (Loop 6 zero-bug convergence) — FINAL STREAK ROUND

- **Audit number:** A31
- **Auditor model:** claude_opus_4_7
- **Branch / HEAD:** `loop6/zero-bug` @ `7767593`
- **Date:** 2026-04-28
- **Streak entering:** 2/3 (A29 zero, A30 zero)
- **Mandate:** Final round to confirm or break the streak. Be more adversarial than A29/A30 — read code paths end-to-end and construct concrete attack/race/data-loss scenarios. Do not rubber-stamp.

---

## Section 1 — Re-probe of recent fixes (W-01, X-01, V-01, V-02, U-01, U-02, U-03, T-01)

| Fix | Sibling/regression angle considered | Result |
|-----|-------------------------------------|--------|
| W-01 (`make_redis_client` factory) | Searched for `redis.Redis(`, `aioredis.Redis(`, `StrictRedis(`, `ConnectionPool` and any `from_url` callsite under `mariana/`. The only producer is the factory; cache.py calls it; api.py + main.py call it. All three callsites import the validator. No subclass override. | NONE |
| X-01 (`_load_rate_limit_storage_uri`) | Re-checked for alternative `Limiter(...)` callsites, alternate env vars (`RATELIMIT_STORAGE_URL/URI`), `storage_options` kwargs override, two-coroutine import race, fork preload. Confirmed — only one Limiter site, no kwargs that override `storage_uri`. | NONE |
| V-01 (`assert_local_or_tls`) | Probed scheme parsing: uppercase `REDIS://`, IPv6 zone-id `[::1%eth0]`, `0.0.0.0`, `redis+sentinel://`. All correctly rejected or accepted per the contract. | NONE |
| V-02 (vault worker fail-closed reservation refund) | Re-read the worker's `try/finally` ordering in `agent/loop.py:1175-1488`. Confirmed `ctx_handle.reset()` and `clear_vault_env(redis, task.id)` always run because the outer try/finally encloses every early-return. Vault env is never durably persisted plaintext after task end. | NONE |
| U-01 (Stripe pending reversals) | Migration 022 is missing a revert script (see Section 4 — operational nit, not security). Reconcile-on-grant path looks correct: `_reconcile_pending_reversals_for_grant` dedupes via `applied_at` + `process_charge_reversal`'s reversal_key. | One nit (Y-02 P4 ops, see findings) |
| U-02 (Decimal billing precision) | Checked all `int(usd*100)` callsites for usage outside the settlement boundary — only RESERVATION amounts use float math, and they reconcile through `usd_to_credits` at settlement. No drift survives to the final balance. | NONE |
| U-03 (vault Redis transport) | The fail-closed branches in `loop.py:1185-1217` for `VaultUnavailableError` / `ValueError` / generic `Exception` (with `requires_vault=True`) all propagate to the outer `finally`, which calls `_settle_agent_credits` — refund honoured. | NONE |
| **T-01 (agent settlement marker-loss replay)** | **The fix routed agent settlement through idempotent `grant_credits` / `refund_credits` with `(ref_type, ref_id)` dedup AND added `agent_settlements.ledger_applied_at`. But the **legacy investigation settlement** in `mariana/main.py:_deduct_user_credits` (called from `_run_single` for `research_tasks`) was NOT migrated. It still calls the non-idempotent `add_credits(p_user_id, p_credits)` and `deduct_credits(target_user_id, amount)` RPCs directly with no claim row, no `ref_type`, no `ref_id`, and `research_tasks` has no `credits_settled` flag.** | **Y-01 (P2) — see findings** |

---

## Section 2 — Re-verification of A29 / A30 "clean" surfaces

Spot-checked five surfaces flagged clean by prior rounds:

1. **Conversation deletion cascade (A29 row 1).** Re-read `delete_conversation` (api.py:2644-2676). Owner-scoped Supabase REST DELETE. Verified — agent_tasks have no FK to conversations (the `conversation_id` is a tag stored on agent_tasks but never used to surface data via the conversations API). NONE.

2. **Vault DELETE silent-204 (A29 row 9).** Re-read `delete_secret` in `vault/store.py:438-457`. PostgREST DELETE with `id=eq.{secret_id}&user_id=eq.{user_id}` — both filters applied. Foreign secret_id matches 0 rows → 204 (no leak). Confirmed.

3. **Settlement reconciler concurrency (A29 row 15).** Re-traced `reconcile_pending_settlements` in `agent/settlement_reconciler.py`. Atomic claim via `UPDATE...SET claimed_at=now() WHERE claimed_at < now() - interval` and inner `FOR UPDATE SKIP LOCKED`. Two reconcilers see disjoint candidate sets. T-01 short-circuits ledger replay via `ledger_applied_at`. NONE.

4. **Pagination cursor (A30 row 4).** Re-read `evidence_ledger.py:203` and `credibility.py:353`. Cursor is `f"{ts}|{item_id}"`, no user_id embedded; task_id from URL is parameterised; bad-cursor falls back to first page (still task-scoped). NONE.

5. **`asyncio.create_task` lifecycle (A30 row 1).** Re-checked the agent queue daemon's `active` set pruning at `main.py:809-848`. `t.done()` filter + `discard` happens at every loop iteration. NONE.

All five spot-checks confirmed.

---

## Section 3 — Brand-new surfaces probed

| # | Surface | Probed | Result |
|---|---------|--------|--------|
| 1 | Test/dev backdoors in prod code | `grep DEBUG\|TEST_MODE\|test@\|impersonate\|act_as\|x-user-id\|on-behalf-of` | Only `DEBUG` env var → CORS dev origins. No backdoor auth path. NONE. |
| 2 | Secrets in repo (committed creds) | `git log --all -p` against `sk_live_`/`api_key=`/`secret=` literal patterns | No real `sk_live_` or `pk_live_` literals committed. Test fixtures use placeholder values. NONE. |
| 3 | DB migration revert convention | `ls frontend/supabase/migrations/` — migrations 004-021 each ship a paired `_revert.sql`. **Migration 022 (U-01) was added without a revert script**, breaking the established convention. | **Y-02 (P4) — see findings** |
| 4 | Outbox / Stripe-DB atomicity | Stripe webhook → `_grant_credits_for_event` writes credit_transactions and patches profile. Two-phase idempotency (B-03) ensures retry safety. The order is: claim webhook event 'pending' → run handler → finalise event 'completed'. Per-grant `(ref_type=event_id)` UNIQUE index prevents double-grant on retry. | NONE |
| 5 | Cookie / CORS / CSP / X-Frame-Options | `SecurityHeadersMiddleware` (api.py:517-545) sets X-Content-Type-Options=nosniff, X-Frame-Options=DENY, X-XSS-Protection, HSTS, CSP=`default-src 'self'`. CORS uses explicit allow_origins list; localhost only in dev. | NONE |
| 6 | Memory exhaustion via large request bodies | No explicit Content-Length cap in code. FastAPI/Starlette default streaming + Pydantic field length limits (goal=16K, instructions=8K). File uploads stream-cap at 10MB per file. Industry-standard defence at proxy layer (nginx/CloudFlare). | NONE (out-of-scope; documented protection at proxy layer) |
| 7 | Admin impersonation / acts-as feature | `grep impersonate\|act_as\|x-on-behalf-of` — no impersonation feature exists. Admin endpoints are `Depends(_require_admin)` + service role on Supabase, no act-as user_id override. | NONE |
| 8 | Multi-tenancy `contextvars` cross-leak | Each `_run_one(task_id)` runs in `asyncio.create_task` (PEP 567 fork-on-Task). Vault env is per-task. The `finally` at `loop.py:1421-1488` resets the context handle. | NONE |
| 9 | WebSocket auth | No `@app.websocket` / `@router.websocket` endpoints exist. Real-time is SSE-only with HMAC stream tokens. | NONE |
| 10 | **Legacy investigation settlement non-idempotency on resume** | `_deduct_user_credits` in `mariana/main.py:412-516` calls `add_credits` / `deduct_credits` RPCs directly. Daemon resume path at `main.py:944-1024` re-runs `_run_single_guarded` for `.running` files left after a crash, which re-invokes `_deduct_user_credits`. The legacy path has NO claim-row, NO `(ref_type, ref_id)` keying, NO `research_tasks.credits_settled` flag. | **Y-01 (P2) — see findings** |
| 11 | Slow-loris / streaming read timeout | SSE endpoint `stream_logs` does `request.is_disconnected()` check inside the loop and breaks. No infinite hangs from misbehaving clients. | NONE |
| 12 | Subdomain takeover / DNS rebinding | The `redis://redis` short-name is the only DNS-trusted hostname (docker service convention). No mariana subdomain handling in this repo. Out of scope. | NONE |
| 13 | Multi-region replication lag (read-after-write) | Mariana uses a single Postgres pool; no read-replica routing. Stripe webhook writes go to the same pool. Out of scope for this codebase. | NONE |
| 14 | Dependency CVEs | No `requirements.txt` audit was performed in this round (would require external CVE feed). Out of scope unless specific known CVE flagged. | NONE (out of scope) |

---

## Section 4 — Findings

| Bug ID | Priority | File:line | Evidence | Suggested fix |
|--------|----------|-----------|----------|---------------|
| **Y-01** | **P2** | `mariana/main.py:412-516` (`_deduct_user_credits`); resume trigger at `mariana/main.py:944-1024` | The legacy investigation settlement in `_deduct_user_credits` calls the **non-idempotent** ledger primitives `POST /rest/v1/rpc/deduct_credits` (`json={"target_user_id": user_id, "amount": delta_tokens}`, line 461-465) and `POST /rest/v1/rpc/add_credits` (`json={"p_user_id": user_id, "p_credits": refund_tokens}`, line 487-491). The DB function bodies (migration 007) are plain `UPDATE profiles SET tokens = tokens ± n` — no `ref_type` / `ref_id` / unique-index dedup. T-01 fixed the AGENT path by routing through `grant_credits` / `refund_credits` with `(ref_type, ref_id)` dedup AND adding `agent_settlements.ledger_applied_at`. The legacy investigation path was NOT migrated and `research_tasks` has no `credits_settled` flag. **Reproducer:** (1) Submit an investigation; reservation R is deducted at submission. (2) Investigation runs to completion through the orchestrator. (3) `_run_single` calls `_deduct_user_credits(reserved=R, final=A1)` — RPC succeeds, applies delta1=A1−R. (4) Daemon process is SIGKILL'd (OOM, k8s pod replacement, oom_score_adj) BEFORE `task_file.rename(.done)` at `main.py:738`. (5) Daemon restarts. The `.running` file is picked up at `main.py:944-1010`. (6) `_run_single_guarded` is called again with the SAME `reserved_credits=R` from the file. (7) `_run_single` enters the orchestrator; orchestrator restores `cost_tracker.total_spent` from `ai_sessions` (BUG-0042), restores `task.current_state = HALT` from the latest checkpoint. The main loop body is skipped. (8) `_run_single` calls `_deduct_user_credits(reserved=R, final=A1)` — same delta1=A1−R applied AGAIN. **Net financial impact:** if A1 < R, user is double-refunded R−A1 (under-bill); if A1 > R, user is double-charged A1−R (over-bill). Window between settle-RPC return and file rename is small (~milliseconds) but a SIGKILL inside that window is realistic in containerised deployments. The agent-side T-01 fix proves this defect class is non-theoretical. | **Apply T-01-style fix to the legacy investigation path:** (a) add `research_tasks.credits_settled BOOLEAN DEFAULT FALSE` column (migration); (b) add a `research_settlements(task_id PK, claimed_at, ledger_applied_at, completed_at)` claim-row table mirroring `agent_settlements` (migration); (c) refactor `_deduct_user_credits` to (i) INSERT INTO `research_settlements` with `ON CONFLICT (task_id) DO NOTHING`, (ii) only proceed if the INSERT created a row OR an existing row has `ledger_applied_at IS NULL`, (iii) call `grant_credits(p_source='refund', p_ref_type='research_task', p_ref_id=task_id)` for delta<0 / `refund_credits(p_ref_type='research_task_overrun', p_ref_id=task_id)` for delta>0 — both already idempotent on `(ref_type, ref_id)` per T-01, (iv) stamp `ledger_applied_at=now()` after a 2xx, (v) UPDATE `research_tasks.credits_settled=TRUE` and stamp `completed_at`. Add regression test mirroring `test_t01_marker_loss_no_replay.py` for the research-task path. |
| **Y-02** | **P4** | `frontend/supabase/migrations/022_u01_stripe_pending_reversals.sql` | Migration 022 (U-01 fix) ships without a paired `022_revert.sql`. Migrations 004 through 021 all have paired revert scripts (verified via `ls frontend/supabase/migrations/`). U-01 added a new table `stripe_pending_reversals` plus two partial indexes; rollback would require operator-authored ad-hoc SQL because no scripted revert exists. Operational risk (cannot cleanly roll back U-01 if a regression surfaces) but no security or runtime correctness impact. | Add `frontend/supabase/migrations/022_revert.sql` with `BEGIN; DROP INDEX IF EXISTS idx_stripe_pending_reversals_pi_unapplied; DROP INDEX IF EXISTS idx_stripe_pending_reversals_charge_unapplied; DROP TABLE IF EXISTS public.stripe_pending_reversals; COMMIT;`. |

---

## Section 5 — Verdict

**TWO FINDINGS.** Streak resets to **0/3**.

- **Y-01 (P2)** — Legacy investigation settlement is non-idempotent on daemon resume, mirroring T-01's exact defect class which T-01 only fixed for the agent path. Real reproducible double-bill / under-bill window between settle-RPC return and `.running`→`.done` file rename when daemon is SIGKILL'd.
- **Y-02 (P4)** — Migration 022 missing revert script (operational gap, not security).

Y-01 is the critical finding. The user's "billion-dollar product" mandate explicitly excludes financial-correctness defects of this exact class — T-01 was identified and fixed in re-audit #17 with full TDD, but the symmetric legacy investigation path was overlooked. A29 and A30 marked T-01 territory clean without re-checking that the fix covered both settlement code paths (agent and research). This is exactly the gap the gate-round mandate was designed to catch.

The streak does NOT close. Iterate again: fix Y-01 with a T-01-style `research_settlements` claim-row + idempotent ledger primitives; ship `022_revert.sql`; re-audit.
