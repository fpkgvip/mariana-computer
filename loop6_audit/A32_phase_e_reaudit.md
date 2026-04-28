# A32 — Phase E Re-audit #27 (Loop 6 zero-bug convergence)

- **Audit number:** A32
- **Auditor model:** gpt_5_4 (delegated; Claude Opus 4.7 executor)
- **Branch / HEAD:** `loop6/zero-bug` @ `2673b8f`
- **Date:** 2026-04-28
- **Streak entering:** 0/3 (Y-01/Y-02 just landed in re-audit #26)

---

## Section 1 — Y-01 / Y-02 fix probe

### Methodology

1. Read `mariana/main.py:_deduct_user_credits` (lines 489-752) end-to-end after the refactor.
2. Read `mariana/main.py:_claim_research_settlement`, `_mark_research_ledger_applied`, `_mark_research_settlement_completed` (lines 412-486).
3. Read `mariana/research_settlement_reconciler.py` (170 lines) end-to-end.
4. Read `mariana/data/db.py:_SCHEMA_SQL` for the new `research_settlements` DDL and `research_tasks.credits_settled` ALTER (lines 130-170).
5. Read `tests/test_y01_research_settlement_idempotency.py` (4 tests).
6. Read `frontend/supabase/migrations/022_revert.sql` against the forward migration.
7. Repo-wide greps:
   - `rpc/add_credits` / `rpc/deduct_credits` — only the api.py reservation helpers and S-01 test guards remain. No settlement-path use.
   - `_deduct_user_credits` callsites — only `mariana/main.py:_run_single` (3 sites: success, KeyboardInterrupt, generic exception). All thread `task_id=task.id, db=db`.
   - `tokens = tokens` / `UPDATE profiles` in Python — only inline string comments, no actual mutations outside the SECURITY DEFINER RPC bodies.
   - `_mark_research_settlement_completed` callsites — `_deduct_user_credits` (3 paths: marker-fixup short-circuit, delta=0 noop, post-RPC finalisation) plus the reconciler's marker-fixup branch.

### Y-01 fix probe — angles considered

| Angle | Result |
|-------|--------|
| Lingering non-idempotent `add_credits` / `deduct_credits` in research path | NONE — `mariana/main.py` no longer calls either; the sole consumers (`_supabase_add_credits` / `_supabase_deduct_credits` in `api.py:7279, 7416`) are bounded to *reservation* paths during a single HTTP request, not settlement. |
| Ordering: claim → RPC → ledger_applied_at → completed_at | Verified. Claim row goes in BEFORE any RPC. After 2xx, `_mark_research_ledger_applied` (single statement, idempotent under `IS NULL` filter) stamps first; only then does `_mark_research_settlement_completed` stamp `completed_at` AND flip `research_tasks.credits_settled`. A crash between any two steps is recoverable: reconciler short-cuts via `ledger_applied_at IS NOT NULL`, idempotent RPC handles a worst-case re-issue. |
| Missed callsites of `_deduct_user_credits` | All 3 callsites (success at line 831, KeyboardInterrupt at 841, generic exception at 858) thread `task_id=task.id, db=db`. No other callers in the repo. |
| `research_tasks.credits_settled` divergence from `research_settlements.completed_at` | Both UPDATEs run inside `conn.transaction()` in `_mark_research_settlement_completed`, so they commit or rollback atomically. After commit, `completed_at IS NOT NULL` ⇒ `credits_settled = TRUE`. The `WHERE … completed_at IS NULL` and `WHERE … credits_settled = FALSE` filters keep both updates idempotent on retry. FK `ON DELETE RESTRICT` prevents deleting `research_tasks` while a `research_settlements` row exists, so the second UPDATE cannot silently match 0 due to a missing parent row. |
| Reconciler concurrency | Atomic `UPDATE research_settlements SET claimed_at = now() WHERE task_id IN (SELECT … FOR UPDATE SKIP LOCKED LIMIT $2)` — same pattern as T-01. Concurrent reconcilers see disjoint candidate sets because the WHERE clause filters by `claimed_at < now() - interval` and the bump puts them outside the threshold. The marker-fixup short-circuit on `ledger_applied_at IS NOT NULL` does NOT issue an RPC. |
| Schema idempotency | `ALTER TABLE … ADD COLUMN IF NOT EXISTS credits_settled BOOLEAN NOT NULL DEFAULT FALSE` backfills existing rows with FALSE. `CREATE TABLE IF NOT EXISTS research_settlements …` and `CREATE INDEX IF NOT EXISTS …` are pure idempotent. Safe to re-run on every startup. |
| Reservation refund-on-failure paths in `api.py` | `_supabase_add_credits` calls at api.py:2970, 3023, 3203, 3215, 3234 are reservation refunds bounded to a single HTTP request — symmetric INSERT-then-undo, no daemon-resume hazard. Out of scope for Y-01. |
| `_atomic_probe_credits` (orchestrator credit probe) | Uses non-idempotent `deduct_credits` + `add_credits` in a probe-then-refund pair. If the daemon is SIGKILL'd between the deduct and the refund, 1 credit is lost per crash event. This is a long-standing design with retry mitigation (3 attempts) and is explicitly documented at `mariana/orchestrator/event_loop.py:3331-3399` (BUG-0040 fix). It is NOT a settlement-double-bill defect — the worst case is a one-credit operator loss with a structured ERROR log for manual reconciliation. Not introduced by Y-01; not in scope. |
| Decimal precision (U-02) survival | `usd_to_credits(total_with_markup)` is still the only int-cast site in `_deduct_user_credits` (line 530). ROUND_HALF_UP semantics preserved end-to-end. Test `test_u02_decimal_billing.py::test_legacy_investigation_quantize` was updated to assert against the new `refund_credits` URL but kept the same quantization expectation (31 credits for $0.305). |
| SIGTERM during settle | Asyncio signal handlers set `_SHUTDOWN` flag; they don't raise into running coroutines. In-flight `await` for RPC POST and marker UPDATE complete normally. The daemon's 120 s grace at `main.py:1457` waits for active tasks before cancelling. Cancellation mid-settle leaves a row with NULL or partial markers — picked up by reconciler within 5 min + 60 s. |
| `delta_tokens == 0` zero-amount RPC | Branch at line 609 explicitly skips RPC for delta=0. `_mark_research_settlement_completed` stamps both markers via `COALESCE(ledger_applied_at, now())`. No zero-amount RPC issued, no idempotency edge case. |
| Supabase ↔ backend Postgres split-brain | Claim row in backend PG; ledger mutation in Supabase. If RPC succeeds but marker UPDATE fails, `ledger_applied_at IS NOT NULL` plus reconciler short-cut prevents replay. If claim INSERT succeeds but RPC fails, idempotent retry by reconciler is safe because `grant_credits` / `refund_credits` dedupe on `(ref_type, ref_id)` against `credit_transactions`. No two-phase commit needed because the live ledger primitives are themselves idempotent. |

### Y-02 fix probe

`022_revert.sql` matches the formatting of `021_revert.sql`. All three statements use IF EXISTS; transactional inside BEGIN…COMMIT. The DROP INDEX before DROP TABLE is conservative — `DROP TABLE` would cascade to the indexes anyway, but the explicit `DROP INDEX IF EXISTS` first is defensive and idempotent. Verified index names against the forward migration:
- `idx_stripe_pending_reversals_charge_unapplied` ✓ (forward at 022:47)
- `idx_stripe_pending_reversals_pi_unapplied` ✓ (forward at 022:51)
- `public.stripe_pending_reversals` ✓ (forward at 022:31)

### Findings — Y-01 / Y-02 fix

NONE.

---

## Section 2 — New-surface sweep

| # | Surface | Probed | Result |
|---|---------|--------|--------|
| 1 | Daemon `.running` resume non-idempotent side effects beyond credits | The orchestrator restores `cost_tracker.total_spent` from `ai_sessions` (BUG-0042) and `current_state` from the latest checkpoint. Re-running with state at HALT skips the loop body. Non-credit side effects (LLM calls, file writes, third-party calls) are gated by the state machine and the cost_tracker — duplicate calls would only happen if checkpointing is incomplete, which is orthogonal to Y-01. No new defect surfaced. | NONE |
| 2 | User-cancel vs daemon-settle race | `/api/investigations/{task_id}/kill` (api.py:3386-3448) sets `research_tasks.status = HALTED` + Redis pubsub. It does NOT touch credits. The daemon's orchestrator detects HALTED via the periodic DB-status check, exits the main loop, and `_run_single` falls through to its terminal-state settle — exactly one `_deduct_user_credits` call per task lifecycle. No double-bill. | NONE |
| 3 | `init_schema` partial-failure | `init_schema` is called BEFORE daemon dispatch in `main()` at line 1545. If any statement fails, the exception propagates and the daemon process dies. Each statement uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS so partial application is safe to re-run. `_ensure_db_modules` only catches `ImportError`, not generic exceptions — schema errors fail loud. | NONE |
| 4 | Supabase + backend Postgres split-brain | See Section 1 — handled because live ledger primitives are idempotent on (ref_type, ref_id), so retry across a Supabase/PG outage is safe. | NONE |
| 5 | JSON typing on `.running` resume | `.running` JSON contains `task_id` (str), `user_id` (str-UUID), `reserved_credits` (int), `budget` (float). All native JSON types. `_normalize_daemon_task_payload` validates types. No type-mismatch hazard on the new `research_settlements` path because all fields flow through TEXT or BIGINT — both round-trip cleanly through asyncpg. | NONE |
| 6 | U-02 Decimal precision survival in refactored `_deduct_user_credits` | `usd_to_credits(total_with_markup)` is the sole int-conversion at line 530. `test_u02_decimal_billing.py::test_legacy_investigation_quantize` updated to the new RPC surface; the quantization assertion (31 for $0.305) is preserved. ROUND_HALF_UP semantics intact. | NONE |
| 7 | SIGTERM during settle | Asyncio signal handlers only set the shutdown flag — they do NOT cancel in-flight `await` calls. Settlement RPC POST + marker UPDATE run to completion. Worst case under SIGKILL between them: reconciler short-cut. | NONE |
| 8 | `grant_credits` / `refund_credits` zero-delta | `delta_tokens == 0` branch at line 609 skips RPC entirely; calls `_mark_research_settlement_completed` to stamp both markers atomically. No zero-amount RPC issued. | NONE |
| 9 | Other non-idempotent ledger callsites repo-wide | `grep rpc/add_credits\|rpc/deduct_credits` returns: `api.py:7294, 7441` (reservation helpers, single-request bounded), `tests/test_s01_rpc_signature_match.py` (regression guard against legacy use). `_atomic_probe_credits` uses the legacy primitives but as a deduct+refund probe with retry — design choice, P4 ops only. | NONE |
| 10 | Reconciler `_deduct_user_credits` re-entry | The reconciler reconstructs a synthetic `_ReplayCostTracker` whose `total_with_markup = final_credits / 100`. `_deduct_user_credits` looks up the existing claim, finds `ledger_applied_at IS NULL` (the only path that reaches the reconciler-replay branch — `ledger_applied_at IS NOT NULL` is short-cut earlier), falls past the claim INSERT (`existing is not None`), reaches the RPC issuance, and the live ledger dedupes on `(ref_type, ref_id=task_id)` so a worst-case replay returns `status='duplicate'`. | NONE |
| 11 | `_mark_research_settlement_completed` cross-row consistency | Both UPDATEs run inside `conn.transaction()`. Atomic. `completed_at IS NOT NULL` and `credits_settled = TRUE` cannot diverge across a successful commit. | NONE |
| 12 | Concurrent same-process settle (two coroutines) | Coroutine A: SELECT returns None → INSERT wins → RPC → markers. Coroutine B: SELECT returns None → INSERT loses (ON CONFLICT DO NOTHING returns no row) → "claim_lost" log + return. No double-RPC. | NONE |

---

## Section 3 — Findings

(empty)

---

## Section 4 — Verdict

**ZERO FINDINGS.** A32 / Phase E re-audit #27 of HEAD `2673b8f`:

- Y-01 fix code: clean. The mirror of T-01 covers the legacy investigation path with the same once-only fence: claim row + idempotent ledger primitives keyed on `(ref_type='research_task'/'research_task_overrun', ref_id=task_id)` + `ledger_applied_at` durable proof + `completed_at` marker. All three call sites of `_deduct_user_credits` thread `task_id` + `db` through. The reconciler mirrors the T-01 reconciler exactly (FOR UPDATE SKIP LOCKED + marker-fixup short-cut). Schema additions are idempotent. Cross-row consistency between `research_settlements.completed_at` and `research_tasks.credits_settled` is guaranteed by a single explicit `conn.transaction()`.
- Y-02 fix: clean. Revert script matches forward migration's index/table names, uses IF EXISTS, transactional.
- 12 fresh new-surface categories probed: all clean. Highlights — daemon resume side effects beyond credits gated by orchestrator state machine + ai_sessions cost reload (not a Y-01 concern); user-cancel does not touch credits (single settlement per task lifecycle); reconciler-replay path safely re-enters `_deduct_user_credits` because the ledger is idempotent on `(ref_type, ref_id)`; SIGTERM does not cancel in-flight awaits.

Streak advances to **1 / 3** zero-finding rounds toward zero-bug convergence.
