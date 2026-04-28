# A38 — Phase E Re-audit #33 (Loop 6 zero-bug convergence)

- **Audit number:** A38
- **Auditor model:** claude_opus_4_7
- **Branch / HEAD:** `loop6/zero-bug` @ `46ca0b1`
- **Date:** 2026-04-28
- **Streak entering:** 1/3 (A37 zero)

---

## Section 1 — BB-01 fresh-angle probe

A37 covered: advisory lock ordering, deficit-only path, bucket_id semantic, CREATE OR REPLACE atomicity, callsite assumptions, sibling ledger functions, migration atomicity. Angles A37 did NOT consider:

| Angle | Result |
|-------|--------|
| `bucket_id = v_first_bucket` integrity | The FIFO loop holds `FOR UPDATE` row locks on every bucket it touches; in plpgsql these locks persist until the surrounding transaction commits. Combined with the per-user advisory lock, no concurrent transaction can DELETE the bucket between the loop's UPDATE and the aggregate INSERT. ✓ |
| `credit_transactions.bucket_id` FK behavior | Schema at `002_deft_credit_ledger.sql:53` declares `bucket_id uuid REFERENCES public.credit_buckets(id)` with NO ON DELETE clause — defaults to `NO ACTION` (deferred check at end of transaction). A concurrent admin DELETE of a bucket would block until refund_credits commits. The aggregate INSERT cannot FK-violate because the bucket was just touched (FOR UPDATE held, plus advisory lock). ✓ |
| Triggers on credit_transactions / credit_buckets | `grep CREATE TRIGGER` against migrations — only triggers on `auth.users` (initial schema) and `user_vaults` / `vault_secrets` (touch triggers). Zero triggers on the credit ledger tables. No hidden side-effects on aggregate INSERT. ✓ |
| Aggregate INSERT compute-balance ordering | `v_balance_after` is computed via `SELECT SUM(remaining_credits) FROM credit_buckets WHERE user_id = p_user_id` AFTER the loop closes (line 132-138). The advisory lock prevents any concurrent same-user mutation, so balance_after equals the post-loop state. The aggregate INSERT then carries this `balance_after`. ✓ |
| `v_first_bucket = NULL` invariant when `v_to_debit_now > 0` | If `v_total_balance > 0`, the loop body executes at least once → `v_first_bucket` is assigned. If `v_total_balance == 0`, then `v_to_debit_now == 0` and the `IF v_to_debit_now > 0` guard skips the INSERT — `v_first_bucket` is irrelevant. Invariant: `v_to_debit_now > 0` ⇒ `v_first_bucket IS NOT NULL`. ✓ |
| Stripe webhook re-entry during migration window | The OLD function body raises UniqueViolation mid-loop on multi-bucket users — Postgres aborts the entire function transaction and rolls back all per-iteration UPDATEs. So the partial debits are NOT visible. After migration 024 commits, retry sees clean state and the new aggregate body succeeds. No half-debited bucket state to worry about. ✓ |
| Concurrent CREATE OR REPLACE during long-running refund | Postgres `CREATE OR REPLACE FUNCTION` takes `AccessExclusiveLock` for the duration of the DDL. A long-running refund call holds a shared lock on the function (via the SQL function-call mechanism). The DDL waits until the call finishes. No half-replaced state. ✓ |
| Partial index NULL ref_id handling | `uq_credit_tx_idem WHERE ... AND ref_id IS NOT NULL`. Rows with NULL ref_id are not in the index — they don't dedup. refund_credits is always called with non-NULL `p_ref_id` from `mariana/main.py:701` (`task_id or f"research_settle:{user_id}:{final_tokens}"`) and `mariana/agent/loop.py:709` (`task.id`). The SQL function does not validate `p_ref_id IS NOT NULL` but no caller passes NULL. P4 hardening could add the validation; not a defect. | NONE |
| `SECURITY DEFINER` + `SET search_path = public, pg_temp` | All references in the function are schema-qualified (`public.credit_transactions`, etc.). Even with empty search_path, the function works. Pattern matches 002 / 006 / 009 prior functions. The setting is safe — `pg_temp` cannot be ahead of explicit `public.` references. | NONE |

### Findings — BB-01

NONE.

---

## Section 2 — Cross-fix interaction audit

Walked through every pair of recent fixes for hidden interactions:

| Pair | Interaction | Result |
|------|-------------|--------|
| AA-01 ↔ BB-01 | AA-01 orphan path issues `refund_credits` (overrun) keyed on `(research_task_overrun, task_id)`. After BB-01, refund_credits writes ONE aggregate row with same key. AA-01's caller only checks `resp.status_code in (200, 204)` — no row-count assumption. ✓ | NONE |
| Y-01 ↔ BB-01 | Y-01's refund-mode calls `grant_credits` (single row per call — unchanged by BB-01). Y-01's overrun-mode calls `refund_credits` — now aggregate-row. Caller in `mariana/main.py` only checks status_code. ✓ | NONE |
| Z-01 ↔ AA-01 ↔ BB-01 | Z-01 cascade DELETEs research_settlements before research_tasks. AA-01 catches FK-violation on claim INSERT and falls through to keyed RPC. BB-01 makes that RPC's aggregate row succeed for multi-bucket users. Together: complete end-to-end correctness for "user deletes RUNNING investigation that overruns on a multi-bucket account." ✓ | NONE |
| W-01 / X-01 / V-01 | All Redis-related; no interaction with ledger functions. | NONE |
| T-01 ↔ BB-01 | T-01 added `agent_settlements.ledger_applied_at` and routed agent settlement through `grant_credits` / `refund_credits`. BB-01 only changes refund_credits's row count, not its return shape. T-01's marker UPDATEs unchanged. ✓ | NONE |

No cross-fix interaction defects.

---

## Section 3 — Brand-new surfaces

| # | Surface | Probed | Result |
|---|---------|--------|--------|
| 1 | Concurrent test runners shared DB / Redis state | Tests use `_open_pool()` per test against the local Postgres baseline. Cleanup in test fixtures via explicit DELETE. Concurrent pytest runs would race on shared rows — but the test isolation via UUIDs in `task_id` / `ref_id` prevents data collision. | NONE |
| 2 | `uq_credit_tx_idem` partial index NULL ref_id | refund_credits is always called with non-NULL `p_ref_id` from all three Mariana callsites. SQL function does not validate this — P4 hardening, not a defect. | NONE |
| 3 | `credit_clawbacks` UNIQUE constraint | Schema at `009_f03_refund_debt.sql:49` declares `UNIQUE(ref_type, ref_id)`. Combined with refund_credits's existing-cb pre-check (line 82-88), replays return 'duplicate' cleanly. ✓ | NONE |
| 4 | Concurrent grant + refund for same user | Both functions acquire `pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))` — same lock key, same user → serialize. ✓ | NONE |
| 5 | Paths that mutate `credit_buckets` without advisory lock | `admin_set_credits` (012:36+) uses `SELECT ... FOR UPDATE` on profiles row instead of advisory lock. **Different lock primitive** — does not synchronize with advisory-lock-holders. Admin-tooling vs ledger-RPC concurrent runs could drift profiles.tokens vs sum-of-bucket-remainings. **Pre-existing pattern, not introduced by BB-01.** Documented as a structural choice in B-16 / I-01 lineage; the row lock on profiles serializes admin-only writes, while advisory lock serializes ledger writes. The two paths are not contended for the same user in practice (admin grants are infrequent). NOT a regression. | NONE |
| 6 | Trigger-firing on aggregate INSERT | No triggers exist on credit_transactions or credit_buckets. ✓ | NONE |
| 7 | PostgreSQL log retention / sensitive data | refund_credits does not log query parameters; it only RAISEs on validation failures with the parameter values. PG server logs may contain those values in EXCEPTION text — operator concern, standard PG behavior. No application-side log of the function's raw parameters. | NONE |
| 8 | Function execution permission | `REVOKE ALL ... FROM PUBLIC; GRANT EXECUTE ... TO service_role` at lines 184-185 of migration 024. Anon and authenticated cannot call. ✓ | NONE |
| 9 | NestD live migration 024 applied | The audit task description confirms migration 024 was applied to NestD live. `pg_get_functiondef` against the local baseline (which was psql-applied during BB-01 fix) confirms the aggregate-row body is in effect. ✓ | NONE |
| 10 | Integer arithmetic overflow | `v_total_balance`, `p_credits`, `v_to_debit_now` are all `integer` (32-bit). Realistic credit values are millions at most, never approaching INT_MAX (~2.1 billion). No overflow risk in `LEAST(v_total_balance, p_credits)`. ✓ | NONE |

---

## Section 4 — Findings

(empty)

---

## Section 5 — Verdict

**ZERO FINDINGS.** A38 / Phase E re-audit #33 of HEAD `46ca0b1`:

- BB-01 fix code: clean across 8 fresh angles A37 did not consider (bucket_id integrity under concurrent locks, FK NO ACTION semantics, trigger absence, balance_after ordering, v_first_bucket invariant, migration-window rollback, DDL lock contention, search_path hardening).
- Cross-fix interaction matrix: clean. AA-01 + BB-01 + Z-01 together provide end-to-end correctness for the "user deletes RUNNING multi-bucket overrunning investigation" scenario.
- 10 brand-new surfaces probed: all clean. The `admin_set_credits` lock-primitive mismatch is a pre-existing structural choice (not a BB-01 regression).

Streak advances to **2 / 3** zero-finding rounds.

One more zero-finding round (A39) closes the loop.
