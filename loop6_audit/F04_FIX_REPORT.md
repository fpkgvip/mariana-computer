# F-04 Fix Report: Plan / Entitlement Unification

**Finding:** F-04 from Phase E re-audit (`A6_phase_e_reaudit.md`)  
**Severity:** P1 — Financial/Access control  
**Fix applied:** 2026-04-27  
**Branch:** `loop6/zero-bug`

---

## Root Cause

Stripe webhook handlers updated only `subscription_plan`, `subscription_status`, and `subscription_current_period_end`. The investigation-gating logic in `start_investigation` (api.py:2703–2764) reads `profiles.plan` for all entitlement decisions (tier access, duration caps, budget caps, continuous-mode gate). The `update_profile_by_stripe_customer` SQL RPC (migration 007) did not include `plan` in its SET list.

Result: a downgrade or cancel webhook left `profiles.plan='flagship'` (or any paid tier) and the user retained premium entitlements indefinitely.

---

## Fix Overview

Option **(b)** from the audit: every Stripe webhook now also sets `profiles.plan` transactionally alongside `subscription_*`, computed from a canonical `_effective_plan()` mapping function.

---

## Plan-Mapping Table

| `subscription_status`         | `plan` set to                              |
|-------------------------------|--------------------------------------------|
| `active`                      | `subscription_plan` (if known slug) else `free` |
| `trialing`                    | `subscription_plan` (if known slug) else `free` |
| `past_due`                    | `subscription_plan` (if known slug) else `free` — **kept active** during retry window |
| `canceled`                    | `free`                                     |
| `unpaid`                      | `free`                                     |
| `incomplete_expired`          | `free`                                     |
| `paused`                      | `None` / any other                         | `free` |
| `customer.subscription.deleted` event | `free` (immediate downgrade, no grace) |

**Grace-period note:** Downgrade fires immediately on `customer.subscription.deleted` (the definitive Stripe signal). `past_due` retains the paid plan to avoid penalising users during a brief payment retry window; access is revoked only on explicit cancel/delete.

**Known plan slugs:** `starter`, `pro`, `max` (from `_PLAN_BY_ID`). Any unrecognised value falls through to `free`.

---

## Files Changed

### `mariana/mariana/api.py`

1. **Added `_ACTIVE_SUBSCRIPTION_STATUSES` constant** — frozenset `{active, trialing, past_due}` used by the mapping function.

2. **Added `_effective_plan(subscription_status, subscription_plan_id) -> str`** helper immediately before the Stripe webhook helper section. Maps Stripe state to the canonical `profiles.plan` value.

3. **`_handle_checkout_completed`** (lines ~5699–5712): added `plan = _effective_plan("active", plan_id)` to the `update_payload` dict before calling `_supabase_patch_profile`.

4. **`_handle_invoice_paid`** (lines ~5797–5806): added `plan = _effective_plan("active", plan["id"])` to the `patch` dict before calling `_supabase_patch_profile_by_customer`.

5. **`_handle_subscription_updated`** (lines ~5961–5985): extracted `subscription_plan_id` from the subscription object's `items.data[0].price.id` (resolved via `_PLAN_BY_PRICE_ID`), then added `plan = _effective_plan(status, subscription_plan_id)` to `update_payload`.

6. **`_handle_subscription_deleted`** (lines ~5998–6024): added `plan = "free"` to the payload dict alongside `subscription_status = "canceled"`. Comment documents the immediate-downgrade design decision.

### `frontend/supabase/migrations/008_f04_plan_entitlement_sync.sql`

New migration (applies locally and to NestD live):
- Creates `loop6_008_applied` guard table (idempotency marker).
- Replaces `update_profile_by_stripe_customer` to add `plan = COALESCE(payload->>'plan', plan)` to the UPDATE SET list.
- One-shot reconcile DO block: sets `plan = 'free'` for all profiles where `subscription_status NOT IN ('active', 'trialing', 'past_due') AND plan IS DISTINCT FROM 'free'`, then inserts the guard row.
- All functions: `SECURITY DEFINER SET search_path = public, pg_temp`.

### `frontend/supabase/migrations/008_revert.sql`

Revert script: restores `update_profile_by_stripe_customer` to the 007 definition (without `plan`), drops the guard table. **Does not undo the reconcile DML** (a data backup restore would be needed for that).

### `scripts/build_local_baseline_v2.sh`

Added `008_f04_plan_entitlement_sync.sql` to the migration apply loop so local rebuilds include this migration.

### `tests/test_f04_plan_entitlement_sync.py`

New regression test file — 9 tests, all green:
- `test_invoice_paid_updates_plan_field` — asserts `plan='pro'` in the Supabase patch payload.
- `test_subscription_deleted_downgrades_plan_to_free` — asserts `plan='free'` on `customer.subscription.deleted`.
- `test_subscription_canceled_status_downgrades_plan` — asserts `plan='free'` on `subscription.updated` with `status=canceled`.
- `test_past_due_keeps_plan_active` — asserts `plan='pro'` (not `free`) for `past_due` status.
- `test_full_downgrade_flow_blocks_continuous_mode` — confirms POST `/api/investigations` with `continuous_mode=True` returns 403 when `profiles.plan='free'`.
- `test_effective_plan_active_returns_plan_slug` — unit test for `_effective_plan`.
- `test_effective_plan_canceled_returns_free` — unit test for `_effective_plan`.
- `test_effective_plan_unknown_plan_slug_returns_free` — unit test for `_effective_plan`.
- `test_effective_plan_all_known_plans_round_trip` — unit test for all known plans.

---

## Migration Applied to Live NestD (`afnbtbeayfkwznhzafay`)

Applied via Supabase MCP connector (`apply_migration` tool) in two steps (due to `$$` dollar-quote parsing limitation in the tool):

**Step 1:** `008_f04_guard_table` — created `loop6_008_applied` table.  
**Step 2:** `008_f04_update_rpc` — replaced `update_profile_by_stripe_customer`.  
**Step 3:** Reconcile DO block executed via `execute_sql`.

### Pre-migration state (NestD live profiles)
| plan | subscription_status | count |
|------|---------------------|-------|
| flagship | active | 1 |
| flagship | none | 12 |

### Post-migration state (NestD live profiles)
| plan | subscription_status | count |
|------|---------------------|-------|
| flagship | active | 1 |
| free | none | 12 |

**Reconcile result:** 12 profiles that had `plan='flagship'` but `subscription_status='none'` (no active subscription) were correctly downgraded to `plan='free'`. The 1 profile with an active subscription was unchanged.

**Guard table:** `loop6_008_applied` contains 1 row (`applied_at: 2026-04-27T14:01:33Z`), preventing any future accidental re-runs of the reconcile.

---

## Test Results

### Python test suite
```
cd /home/user/workspace/mariana && \
  PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb \
  python -m pytest tests/ --ignore=tests/test_f02_upload_session_race.py \
                          --ignore=tests/test_f03_refund_clawback_debt.py \
                          --tb=short -q

125 passed, 10 skipped
```

The two excluded test files (`test_f02_upload_session_race.py::test_second_call_after_consume_returns_409` and all of `test_f03_refund_clawback_debt.py`) were **pre-existing failures** present on the branch before this fix — confirmed by `git stash` / revert. They are owned by the F-02 and F-03 subagents respectively and are not related to F-04.

F-04 specific tests:
```
python -m pytest tests/test_f04_plan_entitlement_sync.py -q
9 passed in 1.76s
```

### Frontend test suite
```
cd /home/user/workspace/mariana/frontend && npm run test

Test Files  6 passed (6)
      Tests  51 passed (51)
```

---

## No Conflicts with F-02

The F-02 fix touches `start_investigation` upload-session logic (api.py:2839–2878). This fix:
- Added helper functions and constants **before** the webhook section.
- Modified only webhook handler functions (`_handle_checkout_completed`, `_handle_invoice_paid`, `_handle_subscription_updated`, `_handle_subscription_deleted`).
- Did not touch `start_investigation` or any upload-session code paths.

Zero conflicts with the F-02 subagent's work.
