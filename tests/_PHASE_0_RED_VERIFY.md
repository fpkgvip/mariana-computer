# Loop 5 Phase 0 — RED-verify report (B-revised)

This file documents what was reconstructed, what was scoped out,
and the proof that the migrations + contract tests + reconciler
behave correctly.

## Scope

Plan B-revised: do **not** rebuild the lost Phase 0 verbatim
(the prior session targeted the wrong defects, see
`loop5_research/LIVE_STATE_AUDIT.md`). Instead, author one
targeted migration grounded in actual live Supabase state, with
a paired idempotency index, reverters, contract tests, and a
read-only reconciler. RED-verify locally before any staging
apply.

## What got reconstructed

### Migrations (`frontend/supabase/migrations/`)
| File                                           | Purpose                                                                               |
| ---------------------------------------------- | ------------------------------------------------------------------------------------- |
| `004_loop5_idempotency_and_rls.sql`            | Fix R1 (two PERMISSIVE UPDATE policies on profiles), R5 (`admin_set_credits` lacks `search_path`/audit). Pre-flight + final invariant assertions. Auto-detects 004b state. |
| `004b_credit_tx_idem_concurrent.sql`           | Fix R2: `CREATE UNIQUE INDEX CONCURRENTLY uq_credit_tx_idem` covering grant + refund + expiry. |
| `004_revert.sql`                               | Restores both old UPDATE policies and old `admin_set_credits`.                        |
| `004b_revert.sql`                              | Drops `uq_credit_tx_idem`, recreates `uq_credit_tx_grant_ref` CONCURRENTLY.           |

### Contract tests (`tests/contracts/`)
| File                                              | What it asserts                                                                        |
| ------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `C01_profiles_single_update_policy.sql`           | Exactly one UPDATE policy on `profiles`.                                              |
| `C02_profiles_update_locks_subscription.sql`      | A user cannot UPDATE their own `subscription_status` / `subscription_plan` / `subscription_current_period_end`. |
| `C03_credit_tx_idempotency_idx_covers_refund.sql` | `uq_credit_tx_idem` exists with predicate covering grant + refund + expiry.            |
| `C04_credit_tx_refund_replay_blocked.sql`         | Inserting two refunds with the same `(ref_type, ref_id)` raises `unique_violation`.   |
| `C05_admin_set_credits_search_path.sql`           | `pg_proc.proconfig` for `admin_set_credits` contains a `search_path=...` element.    |
| `C06_admin_set_credits_writes_audit.sql`          | Calling `admin_set_credits` writes `audit_log` row with `action='admin.set_credits'`.|
| `G01_admin_set_credits_signature_preserved.sql`   | Always-green guard: signature stays `(uuid, integer, boolean) RETURNS integer`.      |

### Tooling
| File                                       | Purpose                                                                            |
| ------------------------------------------ | ---------------------------------------------------------------------------------- |
| `tools/reconcile_ledger.py`                | READ-ONLY drift detector. Compares `profiles.tokens` vs `SUM(credit_buckets.remaining_credits)` (active only) vs latest `credit_transactions.balance_after`. CLI: `--dsn`, `--limit`, `--since-hours`, `--json`, `--write-report`. Exit 0 clean / 2 drift / 1 error. |
| `tests/tools/test_reconcile_ledger.py`     | 14 unit tests against an isolated `reconcile_test` database.                       |
| `scripts/build_local_baseline.sh`          | Wipes/reseeds `testdb` with the live-faithful baseline.                            |
| `scripts/local_baseline.sql`               | Live-faithful replica: 2 conflicting UPDATE policies, narrow grant-only idx, baseline `admin_set_credits` without `search_path`. |
| `scripts/run_contract_tests.sh`            | Test runner with `expect_red` / `expect_green` modes; G-prefix files are always-green guards. |

### Live-state evidence (`loop5_research/`)
Pulled and checked in: `live_tables.json`, `live_columns.json`,
`live_policies.json`, `live_indexes.json`, `live_all_functions.json`,
`live_admin_set_credits.json`, `live_audit_expire.json`,
`live_credit_rpcs.json`, `live_rpc_bodies.json`. Summary in
`LIVE_STATE_AUDIT.md`.

## RED-verify proof

Run, in order, against the local Postgres at `/tmp:55432`:

```
bash scripts/build_local_baseline.sh
bash scripts/run_contract_tests.sh expect_red          # 6/6 pass + G01 guard
psql -f frontend/supabase/migrations/004_loop5_idempotency_and_rls.sql
psql -f frontend/supabase/migrations/004b_credit_tx_idem_concurrent.sql
bash scripts/run_contract_tests.sh expect_green        # 6/6 pass + G01 guard
psql -f frontend/supabase/migrations/004b_revert.sql
psql -f frontend/supabase/migrations/004_revert.sql
bash scripts/run_contract_tests.sh expect_red          # 6/6 pass + G01 guard
python3 -m pytest tests/tools/test_reconcile_ledger.py # 14/14 pass
```

Last run during this session: all four phases green.

## Real issues identified (live state)

| ID | Severity      | Issue                                                                                | Phase |
| -- | ------------- | ------------------------------------------------------------------------------------ | ----- |
| R1 | high          | Two PERMISSIVE UPDATE policies on `profiles` OR'd; older one omits subscription cols from WITH CHECK. | 1 (004)  |
| R2 | high          | `uq_credit_tx_grant_ref` only covers `type='grant'`; refund/expiry can be replayed. | 1 (004b) |
| R3 | medium-high   | `profiles.tokens` and ledger drift apart.                                            | 2 (api.py) |
| R4 | medium        | Inconsistent admin-auth pattern (`auth.uid()` vs `p_caller`).                       | partial in 004 |
| R5 | medium        | `admin_set_credits` lacks `SET search_path = ''`, doesn't write to audit_log.       | 1 (004)  |
| R6 | medium        | `add_credits` / `deduct_credits` skip ledger.                                        | 2 (api.py) |
| R7 | low           | `expire_credits` already has per-user advisory lock — mostly mitigated.             | skipped  |

## Pre-flight verified on live

- Zero existing duplicates for `(ref_type, ref_id, type)` where
  `type IN ('grant','refund','expiry')` — adding `004b`'s wider
  unique index requires no dedupe step.
- Confirmed both UPDATE policies still exist on `profiles`.

## What was NOT reconstructed (deferred)

The prior Phase 0 referenced these artifacts; the source slices
were unrecoverable after the sandbox recycle. They are deferred:

| Artifact                                | Status   | Rationale                                                                                          |
| --------------------------------------- | -------- | -------------------------------------------------------------------------------------------------- |
| 130-bug spec                            | deferred | Source-of-truth slices unrecoverable. A fresh audit will be done as part of Phase 2 planning.     |
| 74-row SQL regression suite             | deferred | Phase 2 model: each `api.py` fix lands with one new regression alongside it.                      |
| 49 pytest tests                         | deferred | Same as above — written when each related code path is touched.                                   |
| 13 vitest tests                         | deferred | Frontend untouched until Phase 3+.                                                                  |
| 5 chaos tests                           | deferred | Will run in CI once api/migration loops are stable.                                                |
| Migrations 006 / 006b / 006c / 007      | replaced | Their D1-D4 spec was wrong (see LIVE_STATE_AUDIT.md). Replaced by 004 + 004b + reverters.         |
| `tools/dedupe_credit_transactions.py`   | not needed | Pre-flight on live shows no duplicate `(ref_type, ref_id, type)` rows in scope.                |
| `tools/reconcile_profiles_tokens.py`    | merged   | Rolled into `reconcile_ledger.py`.                                                                  |
| Contract tests for `rpc_grants` / `search_path` / `plan_canonical_ids` / `dependents` | partial | C05 covers `search_path` for `admin_set_credits`. The other three concern hypothetical RPCs that are not in live state. |
| CI workflow                             | deferred | DB-level cycle is local-runnable end-to-end. CI will be added with Phase 2.                       |

## Phase 1 next steps (gated)

1. Create Supabase dev branch on project `afnbtbeayfkwznhzafay`.
2. Apply `004` + `004b` to dev branch.
3. Run contract tests against dev branch — must be GREEN.
4. Run `tools/reconcile_ledger.py` against dev branch.
5. Apply `004` + `004b` to NestD prod.
6. Run `reconcile_ledger.py` daily during rollout.

**Hard stop after Phase 1.** No `api.py` changes without explicit Phase 2 approval.
