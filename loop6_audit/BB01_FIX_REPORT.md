# BB-01 Fix Report — refund_credits multi-bucket aggregate ledger row

Status: **FIXED 2026-04-28**
Severity: P2 (Stripe refund + K-02 dispute reversal + AA-01 overrun broken on multi-bucket users)
Branch: `loop6/zero-bug`

## 1. Bug

Phase E re-audit #31 (A36) found that the FIFO bucket-debit loop in
`refund_credits` (defined in `009_f03_refund_debt.sql:155-160`)
INSERTs one `credit_transactions` row PER bucket touched, all sharing
`(p_ref_type, p_ref_id, type='refund')`. The unique index
`uq_credit_tx_idem` introduced in
`004b_credit_tx_idem_concurrent.sql:22-26` covers
`(ref_type, ref_id, type) WHERE type IN ('grant','refund','expiry')`,
so the second loop iteration violates the constraint and the entire
function aborts with `UniqueViolation`.

Reproduced locally:

```
asyncpg.exceptions.UniqueViolationError: duplicate key value violates
unique constraint "uq_credit_tx_idem"
DETAIL: Key (ref_type, ref_id, type)=(test_bb01, ..., refund) already exists.
```

Latent since 004b. Affects every consumer of `refund_credits`:

* B-04 / U-01: Stripe refund webhook on a charge whose user has
  multiple credit buckets — handler raises 500, Stripe retries until
  giving up, user keeps refunded credits.
* K-02: dispute reversal on multi-bucket users.
* AA-01: orphan-overrun on multi-bucket users — reservation never
  claws back.

## 2. Why prior audits did not catch it

The 004b migration's own comment at lines 9-11 explicitly excluded
`'spend'` from the unique index because spend writes per-bucket. The
same exclusion is required for `'refund'` but was overlooked. Existing
tests (e.g. `tests/test_b04_refund_dispute.py`, the K-02 suite, the
U-01 suite, the AA-01 suite) all set up single-bucket scenarios.

## 3. Fix

Two equivalent options were considered:

* **Option A (chosen):** Collapse the per-bucket `INSERT` into a
  single aggregate `credit_transactions` row written AFTER the loop
  with `credits = v_to_debit_now`. Per-bucket movement is still
  recorded in `credit_buckets.remaining_credits`; the audit-trail
  field `metadata.aggregate = true` distinguishes the new row shape
  from the old per-bucket shape if any tooling consumes it. Matches
  the existing `grant_credits` pattern of one ledger row per call.
* **Option B (rejected):** Widen `uq_credit_tx_idem` to exclude
  `type='refund'` the same way `'spend'` is excluded. This would lose
  the durable `(ref_type, ref_id)` dedup at the index layer, requiring
  the function's existing `IF v_existing_tx IS NOT NULL` SELECT to be
  the sole guard. Worse — race-prone if a future caller forgets to
  hold the per-user advisory lock first.

Migration files:

* `frontend/supabase/migrations/024_bb01_refund_credits_aggregate_ledger.sql`
  — replaces `refund_credits` via `CREATE OR REPLACE FUNCTION` with
  the aggregate-row body. Re-applies the existing `REVOKE ALL ...
  FROM PUBLIC; GRANT EXECUTE ... TO service_role;` so the privilege
  posture is unchanged.
* `frontend/supabase/migrations/024_revert.sql` — restores the 009
  per-bucket body verbatim. Documented as "use only if BB-01's
  aggregate-row behaviour needs to be rolled back for triage."

The function's:
* Per-user `pg_advisory_xact_lock` retained (line 71 of new function).
* Existing-tx and existing-clawback short-circuits retained at the
  top (lines 76-89 of new function) — replays return early.
* Deficit/clawback INSERT and `profiles.tokens` sync retained.
* Return shape unchanged: same status string ('reversed' /
  'deficit_recorded' / 'duplicate'), same fields.

The aggregate row's `bucket_id` is set to the FIRST bucket touched
(captured in `v_first_bucket`). The `bucket_id` column is nullable
per `002_deft_credit_ledger.sql:53`, so this is purely an audit
nicety — operators investigating a refund can find the bucket the
debit started from. NULL would also be valid.

The `IF v_to_debit_now > 0 THEN INSERT ...` guard preserves the
prior contract that deficit-only refunds (balance was 0 — no actual
debit) record only a `credit_clawbacks` row and skip the
`credit_transactions` row (the `credits > 0` CHECK constraint would
otherwise reject a zero row).

## 4. Live deploy

Migration 024 is in `frontend/supabase/migrations/`. Standard
Supabase deploy machinery applies it on next deploy. The local
Postgres baseline used by the test suite was updated via
`psql -f frontend/supabase/migrations/024_bb01_refund_credits_aggregate_ledger.sql`.

## 5. TDD trace

### RED at `7d8e5ca`

```
$ python -m pytest tests/test_bb01_refund_multi_bucket.py -x
asyncpg.exceptions.UniqueViolationError: duplicate key value violates
unique constraint "uq_credit_tx_idem"
DETAIL: Key (ref_type, ref_id, type)=(test_bb01, ..., refund) already exists.
```

### GREEN after fix

```
$ python -m pytest tests/test_bb01_refund_multi_bucket.py -x
3 passed in 0.10s

$ python -m pytest --tb=short
406 passed, 13 skipped, 0 failed
```

Baseline was 403 passed; +3 = 406 matches the three new BB-01
regression tests with no other delta.

## 6. Regression tests

`tests/test_bb01_refund_multi_bucket.py`:

1. `test_bb01_multi_bucket_refund_succeeds` — three buckets [10,10,10]
   sum to 30; refund 25; expect bucket1=0, bucket2=0, bucket3=5,
   exactly ONE `credit_transactions` row with `credits=25`,
   `type='refund'`, status='reversed'.
2. `test_bb01_multi_bucket_refund_replay_is_duplicate` — second call
   with the same `(ref_type, ref_id)` returns `status='duplicate'`,
   no second ledger row, bucket balances unchanged.
3. `test_bb01_single_bucket_refund_still_works` — single-bucket
   refund (existing test pattern) succeeds end-to-end with one
   aggregate row.

## 7. Out of scope

* `grant_credits` is unchanged (already writes one row per call).
* `spend_credits` is unchanged (per-bucket writes are intentional;
  excluded from `uq_credit_tx_idem`).
* No NestD-side ALTER on `credit_transactions` columns.
* Stripe refund handler in `mariana/api.py` and the K-02 wrapper
  `process_charge_reversal` consume `refund_credits` only via
  `resp.status_code` and the top-level `status` / `credits` fields —
  the aggregate row does not change those returned fields.
* The agent loop's overrun path (`mariana/agent/loop.py:702`) and
  the legacy investigation overrun path (`mariana/main.py:707`)
  similarly only check `resp.status_code in (200, 204)`.

## 8. Residual risk

* If a deployment is rolled back to the 009 body via `024_revert.sql`,
  BB-01 reappears. Operators must re-apply 024 to restore correctness.
* No data backfill needed — the prior buggy function would have
  ABORTED on multi-bucket refunds, so no malformed multi-row state
  exists in the ledger.
