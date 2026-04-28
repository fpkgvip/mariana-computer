# A37 — Phase E Re-audit #32 (Loop 6 zero-bug convergence)

- **Audit number:** A37
- **Auditor model:** gpt_5_4 (delegated; Claude Opus 4.7 executor)
- **Branch / HEAD:** `loop6/zero-bug` @ `e5cdec9`
- **Date:** 2026-04-28
- **Streak entering:** 0/3 (BB-01 just landed in re-audit #31)

---

## Section 1 — BB-01 fix probe

### Methodology

1. Read `frontend/supabase/migrations/024_bb01_refund_credits_aggregate_ledger.sql` end-to-end (186 lines).
2. Read `024_revert.sql` for rollback fidelity to the 009 body.
3. Walked through every angle the task description called out.

### Angles considered

| Angle | Result |
|-------|--------|
| Race between idempotency pre-check and aggregate INSERT | Per-user `pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))` at line 71 acquired BEFORE the SELECT at line 74. Two concurrent calls for the same user serialize at this lock — the second call sees the first's committed row in the SELECT and returns `'duplicate'`. Different users have disjoint lock keys so no contention. ✓ |
| Ordering of advisory lock vs idempotency check | Advisory lock first (line 71), THEN both idempotency SELECTs (credit_transactions at 74, credit_clawbacks at 82), THEN bucket loop, THEN aggregate INSERT. Mirrors `grant_credits` ordering exactly. ✓ |
| `v_to_debit_now == 0` (all-deficit) case | Aggregate INSERT guarded by `IF v_to_debit_now > 0 THEN INSERT ...` at line 145. When balance is 0, no aggregate row written (would violate `CHECK (credits > 0)` anyway). Clawback row IS written at line 161-166. Replay: top check sees clawback row → returns `'duplicate'`. ✓ |
| `bucket_id = v_first_bucket` audit-trail semantic | The aggregate row's `bucket_id` points to the FIRST bucket touched by the FIFO drain. Per-bucket movement is in `credit_buckets.remaining_credits` — the new `metadata.aggregate=true` flag distinguishes the aggregate semantic from the prior per-bucket shape. P4 readability nit — operators need to know about the new shape — but BB01_FIX_REPORT documents it. NOT a defect. |
| Migration order vs concurrent rollouts | `CREATE OR REPLACE FUNCTION` is a metadata DDL operation that takes `AccessExclusiveLock` on the function. In-flight calls of the OLD function complete on the OLD body; new calls AFTER the DDL commit see the NEW body. Postgres serializes — no half-replaced state. ✓ |
| Code rolled out before migration applies | Both old and new code paths only inspect `resp.status_code in (200, 204)`. The OLD function body still exhibits BB-01 on multi-bucket users — but the user explicitly stated migration 024 was applied to NestD live before this audit, so this transient is closed. ✓ |
| Stripe refund webhook consumer assumptions | `mariana/api.py:7110-7127` reads `result.get("status")` and `result.get("credits")` from `process_charge_reversal`'s top-level return, NOT from `refund_credits`'s internal `refund_result`. No row-count assumption. The `metadata.aggregate=true` flag is opaque to the consumer. ✓ |
| `refund_credits` python wrapper at `mariana/billing/ledger.py:152-174` | Returns the raw RPC dict; no row-count assumption. No external caller of this wrapper found. ✓ |

### Findings — BB-01

NONE. The fix correctly serialises per-user, preserves the existing-tx and existing-clawback short-circuits, writes a single aggregate row matching `uq_credit_tx_idem`, and preserves deficit/clawback semantics from F-03.

---

## Section 2 — grant_credits + other ledger functions same-class probe

| Function | INSERT pattern | UNIQUE-violation risk | Result |
|----------|---------------|---------------------|--------|
| `grant_credits` (009:199+) | One INSERT into `credit_buckets` (the new bucket) + one INSERT into `credit_transactions` (`type='grant'`) at line 255-259, then a clawback-satisfaction loop at 268-318 that writes `type='clawback_satisfy'` rows. | `uq_credit_tx_idem` covers `type IN ('grant','refund','expiry')` — `'clawback_satisfy'` is excluded. Multiple clawback_satisfy rows can share `(ref_type, ref_id)` without conflict. The single grant row per call has its own dedup. | NONE |
| `spend_credits` (007:364+) | Per-bucket INSERT in FIFO loop with `type='spend'`. | 004b explicitly excludes `'spend'` from `uq_credit_tx_idem` (the comment at 004b:9-11 notes this). No conflict possible. | NONE |
| `expire_credits` (007:562+) | Per-bucket INSERT in batch loop with `type='expiry'`, `ref_type='bucket'`, `ref_id=bucket_id::text`. | Each iteration has a UNIQUE `ref_id` (the bucket's UUID-as-text), so the `uq_credit_tx_idem` constraint is satisfied per-iteration. WHERE clause on the candidate SELECT excludes already-expired (`remaining_credits > 0`), so a second batch run does not re-pick the same bucket. | NONE |
| `refund_credits` (024 latest) | Single aggregate INSERT after the FIFO loop. | BB-01 fix. ✓ | NONE (post-fix) |
| `process_charge_reversal` (021:37+) | Wraps `refund_credits`; per-charge advisory lock then per-user lock via `refund_credits`. Lock order documented (charge → user). | No new INSERT into credit_transactions; relies on `refund_credits`'s aggregate row. | NONE |
| `add_credits` (018:9+) | Single UPDATE on `credit_clawbacks` per FIFO iteration, plus a single UPDATE on `profiles.tokens`. No `credit_transactions` INSERT. | No conflict surface. | NONE |

Conclusion: BB-01 was the only ledger function with the multi-bucket UNIQUE-violation defect class. All siblings are clean.

---

## Section 3 — Callsites of `refund_credits` in `mariana/`

| Caller | File:line | Result-handling | Aggregate-row compatibility |
|--------|-----------|----------------|---------------------------|
| Legacy investigation overrun | `mariana/main.py:707-715` | Only checks `resp.status_code in (200, 204)`; logs `extra_deducted=delta_tokens`. No row-count or metadata inspection. | ✓ |
| Agent overrun | `mariana/agent/loop.py:702-732` | Only checks `resp.status_code in (200, 204)`; logs `extra_deducted=delta`. No row-count inspection. | ✓ |
| Stripe charge-reversal handler | `mariana/api.py:7060-7127` | Calls `process_charge_reversal` (K-02 wrapper around `refund_credits`); reads `result.get("status")` and `result.get("credits")` from K-02's top-level return. K-02's return is unaffected by the aggregate-row change because K-02 wraps `refund_credits` and embeds the inner result as `refund_result` (opaque to upstream). | ✓ |
| `mariana/billing/ledger.py:152-174` python wrapper | Returns raw dict; no internal post-processing. | ✓ (and unused by other Mariana code per repo grep) |

No caller assumes per-bucket rows. All four are aggregate-row compatible.

---

## Section 4 — New-surface sweep

| # | Surface | Probed | Result |
|---|---------|--------|--------|
| 1 | Migration 024 atomicity vs concurrent function calls | Postgres `CREATE OR REPLACE FUNCTION` takes `AccessExclusiveLock` on the function for the duration of the DDL statement. In-flight calls finish on the OLD body; new calls after commit see the NEW body. No half-replaced state visible to clients. | NONE |
| 2 | `credit_clawbacks` reconciler | Not a separate reconciler — clawbacks are satisfied inline inside `grant_credits` (009:268-318). No async retry path. The `add_credits` function (018) satisfies clawbacks oldest-first when new tokens land. | NONE |
| 3 | Plan upgrade / downgrade ledger calls | Stripe `customer.subscription.updated` handler updates `subscription_status` / `subscription_plan` on `profiles` and grants prorated credits via `_grant_credits_for_event` (calls `grant_credits` directly). Single grant per event-id, idempotent on `(ref_type='stripe_event', ref_id=event_id)`. No multi-bucket refund issue. | NONE |
| 4 | Subscription proration | Stripe sends `invoice.paid` with prorated credits; `_handle_invoice_paid` grants the FULL plan amount keyed on event_id (not prorated — by-design, granted as one bucket per renewal). Single ledger row per event. | NONE |
| 5 | Bucket expiry batch (`expire_credits`) | Per-bucket INSERT with `ref_id=bucket_id::text` — each iteration has a distinct ref_id, no UNIQUE conflict. `WHERE remaining_credits > 0 AND expires_at <= clock_timestamp()` so already-expired buckets are not re-picked. | NONE |
| 6 | Manual admin grant/refund tooling | `_supabase_add_credits` / `_supabase_deduct_credits` in `api.py:7279/7416` are admin reservation helpers (request-bounded, single HTTP request). Not subject to multi-bucket UNIQUE issues because they call the legacy `add_credits` / `deduct_credits` RPCs which write to `profiles.tokens` directly without per-bucket transactions. | NONE |
| 7 | Trigger / audit-log table on `credit_transactions` | No triggers found via `pg_trigger` query and no audit-log table. The unique index `uq_credit_tx_idem` is the sole enforcement. | NONE |
| 8 | `profiles.tokens` sync correctness vs bucket drift | `UPDATE profiles SET tokens = GREATEST(0, tokens - v_to_debit_now)` (refund_credits:170). The `GREATEST(0, ...)` floor masks any drift where `profiles.tokens < v_to_debit_now`. Drift would only occur if some other code path updates `credit_buckets.remaining_credits` without syncing `profiles.tokens` — verified by grep, no such path exists. ✓ | NONE |
| 9 | `balance_after` value semantics | Computed AFTER the FIFO loop completes (line 132-138 of new function). With per-user advisory lock held, no concurrent debit can land between loop completion and the SELECT, so balance_after is correct. ✓ | NONE |
| 10 | Re-verify W-01 / X-01 / Y-01 / Z-01 / Z-02 / AA-01 | All grep checks re-confirmed — no regression introduced by 024. The migration touches only `refund_credits` and re-applies its grants. | NONE |

---

## Section 5 — Findings

(empty)

---

## Section 6 — Verdict

**ZERO FINDINGS.** A37 / Phase E re-audit #32 of HEAD `e5cdec9`:

- BB-01 fix code: clean across 8 fresh angles (race, ordering, deficit, audit-trail, migration order, code-vs-migration sequencing, consumer compatibility, python wrapper).
- Sibling ledger functions audited for the same defect class — none affected (`grant_credits` has only one ledger row per call; `spend_credits` is correctly excluded from the unique index; `expire_credits` uses distinct ref_id per bucket; `process_charge_reversal` wraps `refund_credits`).
- All 4 callsites of `refund_credits` in `mariana/` only inspect HTTP status code; no row-count or metadata assumption.
- 10 fresh new-surface categories swept: all clean.
- W-01 / X-01 / Y-01 / Z-01 / Z-02 / AA-01 re-verification: all hold.

Streak advances to **1 / 3** zero-finding rounds toward zero-bug convergence.

Two more zero-finding rounds (A38, A39) close the loop.
