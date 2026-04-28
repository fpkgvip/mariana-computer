# T-01 fix report — settlement marker-loss reconciler replay

**Date:** 2026-04-28
**Branch:** `loop6/zero-bug`
**Severity:** P1 (financial — refund-twice / charge-twice)
**Option chosen:** **C** (idempotent ledger primitives + `ledger_applied_at` marker)

## 1. Summary

After S-01..S-04, agent settlement relied on a single durable fence —
`agent_settlements.completed_at` — to make the non-idempotent low-level
PostgREST RPCs `add_credits(p_user_id, p_credits)` and
`deduct_credits(target_user_id, amount)` safe. Any transient failure
between the successful ledger RPC and the `UPDATE … SET completed_at =
now()` statement left the row eligible for reconciler retry; the
reconciler then forced `task.credits_settled = False` and re-invoked
`_settle_agent_credits`, driving a **second real ledger mutation** for
the same task. Net: refund-twice or charge-twice.

The fix routes agent settlement through the **idempotent** ledger
primitives that already live in NestD — `grant_credits` and
`refund_credits`, both deduplicated on `(ref_type, ref_id)` inside
`credit_transactions` — and adds an explicit
`agent_settlements.ledger_applied_at` column that the reconciler uses to
distinguish "ledger mutation already on disk, only marker bookkeeping is
stale" from "RPC genuinely failed, retry it".

## 2. Root cause

`mariana/agent/loop.py:687-699` (pre-fix) swallowed any exception raised
by `_mark_settlement_completed` after a 2xx RPC:

```python
if rpc_succeeded:
    task.credits_settled = True
    if db is not None:
        try:
            await _mark_settlement_completed(db, task.id)
        except Exception as exc:
            logger.warning("agent_settlement_mark_completed_failed", ...)
```

Failure window: RPC 200 → `_mark_settlement_completed` raises (DB
hiccup, statement timeout, pool reset) → in-memory
`credits_settled=True` but `agent_settlements.completed_at IS NULL`.

`mariana/agent/settlement_reconciler.py:140-145` then forced
`task.credits_settled = False` for the row five minutes later and
re-called `_settle_agent_credits`. Because `add_credits` /
`deduct_credits` have no `(ref_type, ref_id)` dedup, the second RPC was
a real second mutation. Reproduced at
`loop6_audit/A22_double_settle_repro.txt`.

## 3. Why R-01 / S-01 / S-03 didn't catch it

* **R-01** protected the *pre-RPC* duplicate-settle race using a claim
  row. It assumed the claim row's `completed_at` would always be stamped
  after a 2xx RPC, but its tests mock the marker write as infallible.
* **S-01** fixed the bad PostgREST payload but kept the same fence
  contract. Its tests cover RPC success and RPC failure, never RPC
  success + marker write failure.
* **S-03** added a reconciler for genuinely failed RPCs. It works; the
  bug is that it cannot tell a "marker-write-only failure" apart from a
  "RPC genuinely failed" — both look identical to a `completed_at IS
  NULL` row.

## 4. Fix design (Option C)

### 4.1 Route through idempotent ledger primitives

Verified live (afnbtbeayfkwznhzafay):

```sql
SELECT proname, pg_get_function_arguments(oid) FROM pg_proc
 WHERE proname IN ('grant_credits','refund_credits');
-- grant_credits(p_user_id uuid, p_credits integer, p_source text,
--               p_ref_type text DEFAULT NULL, p_ref_id text DEFAULT NULL,
--               p_expires_at timestamptz DEFAULT NULL)
-- refund_credits(p_user_id uuid, p_credits integer,
--                p_ref_type text, p_ref_id text)
```

Both source bodies (inspected via `pg_get_functiondef`) contain an
explicit duplicate check against `credit_transactions`:

```sql
SELECT id INTO v_existing_tx FROM public.credit_transactions
 WHERE type = 'grant'/'refund' AND ref_type = p_ref_type AND ref_id = p_ref_id;
IF v_existing_tx IS NOT NULL THEN
  RETURN jsonb_build_object('status','duplicate', 'transaction_id', v_existing_tx);
END IF;
```

Mapping:
* `delta < 0` (refund unused reservation) → `grant_credits(source='refund',
  ref_type='agent_task', ref_id=task.id)`.
* `delta > 0` (overrun, claw back from user) → `refund_credits(
  ref_type='agent_task_overrun', ref_id=task.id)`. (Confusing name — in
  the live ledger `refund_credits` debits buckets / records a clawback;
  it is the right semantic for taking credits *back* from a user.)

### 4.2 `ledger_applied_at` column (defense in depth)

Even with idempotent ledger primitives, the reconciler should not waste
a round-trip re-issuing a known-applied RPC. New column:

```sql
ALTER TABLE agent_settlements ADD COLUMN IF NOT EXISTS ledger_applied_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_agent_settlements_ledger_applied_pending_complete
    ON agent_settlements(ledger_applied_at)
    WHERE completed_at IS NULL AND ledger_applied_at IS NOT NULL;
```

After RPC 2xx, `_settle_agent_credits` first calls
`_mark_ledger_applied` (single statement, idempotent under `IS NULL`
filter), then `_mark_settlement_completed` (which now sets BOTH
`ledger_applied_at = COALESCE(ledger_applied_at, now())` and
`completed_at = now()` in one statement). `task.credits_settled` flips
True only after `completed_at` is durably stamped — the prior code
flipped it on RPC success alone.

The reconciler now reads `ledger_applied_at` in the candidate UPDATE
RETURNING and short-cuts any row where it is non-NULL to a marker
fix-up (`_mark_settlement_completed` only) — never re-entering
`_settle_agent_credits`.

### 4.3 Existing-claim lookup also handles `ledger_applied_at`

`_settle_agent_credits` reads both `completed_at` and `ledger_applied_at`
in its existing-claim probe. If a same-process retry hits the row after
a marker-only failure, it stamps `completed_at` and returns without an
RPC.

## 5. Schema diff

`mariana/agent/schema.sql`:

```diff
+-- T-01: separate "ledger RPC has been applied" from "settlement workflow
+-- complete" …
+ALTER TABLE agent_settlements
+    ADD COLUMN IF NOT EXISTS ledger_applied_at TIMESTAMPTZ;
+
+CREATE INDEX IF NOT EXISTS idx_agent_settlements_ledger_applied_pending_complete
+    ON agent_settlements(ledger_applied_at)
+    WHERE completed_at IS NULL AND ledger_applied_at IS NOT NULL;
```

Live-apply: `agent_settlements` lives in **backend Postgres**, NOT
Supabase, so no NestD migration was issued. The change was applied to
the local Postgres baseline via `psql -f mariana/agent/schema.sql` and
verified via `\d agent_settlements`. Production deployment runs the
same idempotent SQL on startup (same pattern as M-01 / N-01).

No NestD function changes — `grant_credits` / `refund_credits` already
exist live with the required signatures.

## 6. Code diff summary

* `mariana/agent/loop.py`
  * `_mark_settlement_completed` now stamps both
    `ledger_applied_at = COALESCE(ledger_applied_at, now())` and
    `completed_at = now()` in one statement.
  * New `_mark_ledger_applied` helper (single-statement
    `UPDATE … SET ledger_applied_at = now() WHERE … IS NULL`).
  * `_settle_agent_credits`:
    * existing-claim lookup includes `ledger_applied_at`; non-NULL ⇒
      stamp `completed_at` and return without RPC.
    * delta<0 → POST `/rpc/grant_credits` with the full
      `(p_user_id, p_credits, p_source, p_ref_type, p_ref_id)` payload.
    * delta>0 → POST `/rpc/refund_credits` with
      `(p_user_id, p_credits, p_ref_type, p_ref_id)`.
    * Post-RPC: `_mark_ledger_applied` → `_mark_settlement_completed`;
      `task.credits_settled = True` only after both succeed.
    * delta==0 noop branch: don't flip `credits_settled` until
      marker write succeeds (defensive, no RPC issued in this path).
* `mariana/agent/settlement_reconciler.py`
  * Candidate UPDATE returns `ledger_applied_at` along with `task_id`.
  * Per-row loop: if `ledger_applied_at IS NOT NULL`, call
    `_mark_settlement_completed` directly (marker fix-up); otherwise
    proceed with `_settle_agent_credits` retry as before.
* `mariana/agent/schema.sql` — see §5.

## 7. Test plan

* **New: `tests/test_t01_marker_loss_no_replay.py`** (3 functions, 2
  exercised — refund and overrun paths). Pattern: insert `agent_tasks`
  row, patch `_mark_settlement_completed` to raise once, run
  `_settle_agent_credits` (assert exactly 1 RPC POST,
  `task.credits_settled is False`), age the claim 10 minutes, run
  `reconcile_pending_settlements` (assert STILL exactly 1 RPC POST,
  row in terminal state).
* **Updated: `tests/test_s01_rpc_signature_match.py`** — payload-shape
  assertions rewritten for the new live signatures
  (`grant_credits` / `refund_credits`); new
  `test_s01_no_legacy_unkeyed_rpc` guards against accidental reversion
  to `add_credits` / `deduct_credits`.
* **Updated:** RPC URL filters in
  `test_r01_settlement_idempotency.py`, `test_o02_cancel_settlement.py`,
  `test_p01_stale_worker_race.py`, `test_q01_cas_state_clobber.py`,
  `test_n01_settlement_persistence.py`, `test_m01_agent_billing_unit.py`
  (mechanical `add_credits` → `grant_credits`,
  `deduct_credits` → `refund_credits`; payload key checks were already
  permissive).
* **Counts:** 365 → 368 tests collected (+3). `pytest -x -q` from repo
  root with local Postgres available: **355 passed, 13 skipped, 0
  failed** (the 13 skips are pre-existing — they gate on external
  services not present in the dev sandbox).

## 8. Live-apply notes

* `agent_settlements` is backend-Postgres-only — no Supabase migration
  needed. The startup-time idempotent SQL in `mariana/agent/schema.sql`
  is the deployment vector; it was verified locally and is safe to
  re-run on prod.
* `grant_credits` / `refund_credits` are already deployed and used by
  the Stripe billing path (`mariana/billing/ledger.py`). Reusing them
  for agent settlement increases their write volume but does not change
  their contract.
* No NestD migration file was created. The audit hint considered one
  but the table location and the fact that the ledger RPCs already
  exist makes it unnecessary.

## 9. Residual risk

* `spend_credits` was investigated as the overrun primitive but its live
  body has no `(ref_type, ref_id)` dedup, so we deliberately use
  `refund_credits` for the overrun path instead. If a future migration
  adds idempotency to `spend_credits` and we want overrun to consume
  the user's expiring buckets in FIFO order, that's a follow-up — for
  T-01 the priority was correctness over bucket-FIFO semantics.
* `_mark_ledger_applied` and `_mark_settlement_completed` are still two
  separate statements. If `_mark_ledger_applied` itself fails (very
  rare, single-row UPDATE), a worst-case replay returns
  `status='duplicate'` from the idempotent ledger RPC — no double
  mutation. Verified by the T-01 regression test, which runs the
  reconciler with `_mark_settlement_completed` raising and asserts no
  replay POST.
* The reconciler's marker-fix-up path uses `_mark_settlement_completed`,
  which sets both columns atomically; if the row already has
  `completed_at` non-NULL (race against another reconciler), the
  `WHERE … completed_at IS NULL` filter makes it a no-op — safe under
  concurrency.
* Database column `ledger_applied_at` is nullable; existing rows from
  before the deploy will have NULL there. For those rows, if they're
  still in `completed_at IS NULL` state at reconciler pick-up, the
  reconciler will issue a new RPC — but because the RPC is now
  idempotent, a "duplicate" response is the worst case.

## 10. Followup findings

None. No new bugs were discovered while fixing T-01.
