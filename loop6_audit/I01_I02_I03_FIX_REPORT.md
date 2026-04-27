# I-01 + I-02 + I-03 Fix Report

**Date:** 2026-04-27  
**Branch:** loop6/zero-bug  
**Base commit:** cc3183b  
**Audit source:** A9_phase_e_reaudit.md

---

## I-01 [P2] — add_credits missing advisory lock

**Root cause:** `public.add_credits` (introduced in `009_f03_refund_debt.sql:347-409`) reads
`v_open_total` from `credit_clawbacks` and computes `v_net_addition` before acquiring any
serialization lock. Every sibling function (`refund_credits` at line 101, `grant_credits` at
line 231, `spend_credits`, `deduct_credits`) acquires
`pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))` before reading. Without the lock,
a concurrent `refund_credits` can commit a `credit_clawbacks` row between `add_credits`'s
initial read and its `FOR UPDATE` loop, causing `add_credits` to use a stale `v_net_addition`
and add the full pre-clawback amount to `profiles.tokens` while also satisfying the clawback.

**Fix:** Migration `018_i01_add_credits_lock.sql` — `CREATE OR REPLACE FUNCTION public.add_credits`
with `PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));` inserted
immediately after the `p_credits < 0` validation guard, matching the pattern in `refund_credits`
and `grant_credits`. All other logic is unchanged.

**Revert:** `018_revert.sql` — restores the lock-less version from 009 (for reference only).

**Tests:**
- `tests/test_i01_add_credits_lock.py` (5 tests):
  1. `test_function_definition_contains_advisory_lock` — queries `pg_get_functiondef` live.
  2. `test_sequential_add_credits_no_clawback_adds_full_amount` — adds 50 credits with no clawback.
  3. `test_sequential_add_credits_with_existing_clawback_nets_correctly` — 100 credits, 60 clawback, expects 40 tokens.
  4. `test_add_credits_raises_on_negative_credits` — regression guard.
  5. `test_migration_018_file_contains_advisory_lock` — file-level text assertion (no DB required).

**Live verification:**
```sql
SELECT prosrc ILIKE '%pg_advisory_xact_lock%' AS has_advisory_lock
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public' AND p.proname = 'add_credits';
-- Result: [{has_advisory_lock: true}]
```

---

## I-02 [P2] — Dispute reversal TOCTOU: concurrent webhooks double-debit

**Root cause:** `_reverse_credits_for_charge` (api.py:6446-6568) calls
`_record_dispute_reversal_or_skip` (SELECT-only check) and, if no row exists, proceeds to
`_refund_rpc(ref_id=event_id)`. Because `event_id` differs between `charge.dispute.created`
(evt_A) and `charge.dispute.funds_withdrawn` (evt_B), `refund_credits`'s idempotency guard on
`(type='refund', ref_type, ref_id)` does not collapse concurrent calls for the same dispute.
Two concurrent webhooks can both pass the SELECT and each succeed in the refund RPC, resulting
in two debits for the same dispute.

**Fix:** api.py only — no DB migration required.

1. Added `_compute_reversal_key(charge_obj, dispute_obj) -> str` helper at api.py line 6324.
   Returns `"dispute:<dispute_id>"` when a dispute object with an id is present, else
   `"charge:<charge_id>:reversal"`. This is the same logic previously duplicated in
   `_record_dispute_reversal_or_skip` and `_insert_dispute_reversal`.

2. `_reverse_credits_for_charge` now computes `reversal_key = _compute_reversal_key(...)` once
   before the SELECT short-circuit, and passes `ref_id=reversal_key` to `_refund_rpc` instead of
   `ref_id=event_id`. `event_id` is still passed to `_insert_dispute_reversal` as `first_event_id`
   for forensic tracing.

3. Both `_record_dispute_reversal_or_skip` and `_insert_dispute_reversal` are refactored to call
   `_compute_reversal_key` instead of duplicating the key logic.

**Double-defense:**
- SELECT fast-path: if a `stripe_dispute_reversals` row already exists, skip immediately.
- RPC dedup: if two concurrent webhooks both pass the SELECT, `refund_credits` collapses the
  second call on `(type='refund', ref_type='stripe_event', ref_id='dispute:<id>')` server-side.

**Tests:**
- `tests/test_i02_dispute_reversal_stable_key.py` (6 tests):
  1. `test_refund_rpc_called_with_reversal_key_not_event_id` — asserts `p_ref_id=dispute:dp_X`.
  2. `test_concurrent_dispute_events_same_dispute_only_one_refund_via_rpc_dedup` — asyncio.gather, second returns 'duplicate', only 1 debit.
  3. `test_record_dispute_reversal_or_skip_still_short_circuits_when_row_exists` — SELECT fast-path still works.
  4. `test_compute_reversal_key_returns_dispute_id_format` — dispute path.
  5. `test_compute_reversal_key_returns_charge_format_when_no_dispute` — charge path.
  6. `test_insert_dispute_reversal_records_first_event_id` — forensic trace preserved.

- Updated `tests/test_b04_refund_dispute.py` — three existing assertions updated from
  `p_ref_id=event_id` to `p_ref_id=reversal_key` to reflect the corrected behavior.

---

## I-03 [P3] — Marker tables publicly readable and writable

**Root cause:** `loop6_007_applied` and `loop6_008_applied` tables in the `public` schema had
`relrowsecurity=false` and granted `INSERT, SELECT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER`
to the `anon` and `authenticated` roles. Any browser with the public anon key could read, write,
or truncate these migration-marker tables.

**Fix:** Migration `019_i03_marker_tables_rls.sql` — a `DO $$` block that, for each marker table
(if it exists):
1. `ALTER TABLE public.<t> ENABLE ROW LEVEL SECURITY` — no permissive policies, so all access is
   denied by default (service_role bypasses RLS).
2. `REVOKE ALL ON public.<t> FROM PUBLIC, anon, authenticated`.
3. `GRANT ALL ON public.<t> TO service_role` — migration runner is unaffected.

The `IF EXISTS` guard makes the migration idempotent and safe to apply in environments where the
tables do not exist.

**Revert:** `019_revert.sql` — re-enables access (for reference only, not for production use).

**Contract:** `tests/contracts/C20_marker_tables_rls.sql` — 5 checks:
1. `loop6_007_applied` has `relrowsecurity=true` (skip-pass if table absent).
2. `loop6_008_applied` has `relrowsecurity=true` (skip-pass if table absent).
3. `anon`/`authenticated` have no grants on `loop6_007_applied`.
4. `anon`/`authenticated` have no grants on `loop6_008_applied`.
5. Informational: service_role access verified via RLS posture.

**Live verification:**
```sql
SELECT relname, relrowsecurity FROM pg_class
 WHERE relname IN ('loop6_007_applied','loop6_008_applied');
-- Result: [{relname: loop6_007_applied, relrowsecurity: true},
--          {relname: loop6_008_applied, relrowsecurity: true}]
```

---

## Files changed

| File | Change |
|------|--------|
| `frontend/supabase/migrations/018_i01_add_credits_lock.sql` | New: add_credits with advisory lock |
| `frontend/supabase/migrations/018_revert.sql` | New: revert to lock-less version |
| `frontend/supabase/migrations/019_i03_marker_tables_rls.sql` | New: RLS + revoke on marker tables |
| `frontend/supabase/migrations/019_revert.sql` | New: revert for completeness |
| `mariana/api.py` | I-02: _compute_reversal_key + ref_id=reversal_key in _refund_rpc |
| `scripts/build_local_baseline_v2.sh` | Added 018 + 019 to migration loop |
| `tests/test_i01_add_credits_lock.py` | New: 5 tests for I-01 |
| `tests/test_i02_dispute_reversal_stable_key.py` | New: 6 tests for I-02 |
| `tests/test_b04_refund_dispute.py` | Updated: 3 assertions use reversal_key |
| `tests/contracts/C20_marker_tables_rls.sql` | New: contract for I-03 |
| `loop6_audit/REGISTRY.md` | I-01/I-02/I-03 marked FIXED 2026-04-27 |

## Test results

- Full suite: 279 passed, 13 skipped (baseline 268 + 11 new)
- Contract suite: 18/18 pass (17 existing + C20)
- build_local_baseline_v2.sh: clean run, migrations 018+019 applied

## Live NestD (project afnbtbeayfkwznhzafay)

- Migration 018: applied, `has_advisory_lock=true`
- Migration 019: applied, `relrowsecurity=true` on both marker tables
