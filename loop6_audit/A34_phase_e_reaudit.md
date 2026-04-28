# A34 — Phase E Re-audit #29 (Loop 6 zero-bug convergence)

- **Audit number:** A34
- **Auditor model:** gpt_5_4 (delegated; Claude Opus 4.7 executor)
- **Branch / HEAD:** `loop6/zero-bug` @ `4a0c50e`
- **Date:** 2026-04-28
- **Streak entering:** 0/3 (Z-01/Z-02 just landed in re-audit #28)

---

## Section 1 — Z-01 fix probe

### Methodology

1. Read `mariana/api.py:delete_investigation` end-to-end after the cascade-list extension.
2. Read `tests/test_z01_research_delete_cascade.py` (3 tests).
3. Cross-checked the cascade list against every `REFERENCES research_tasks` declaration in `mariana/data/db.py` (25 child tables). Cascade list has 26 entries — every FK target is present (+ one no-op `learning_insights` which has no `task_id` column; the silent `try/except` catches the resulting "column does not exist" error harmlessly. Pre-existing dead entry, not introduced by Z-01.)
4. Walked through the daemon-mid-settle race scenarios.

### Findings — Z-01

| Angle | Result |
|-------|--------|
| Cascade order: research_settlements before research_tasks | Verified — entry placed at line 3628 inside the cascade_tables list, BEFORE the trailing `DELETE FROM research_tasks` at line 3640. ✓ |
| User-deletes-completed-task | Tests pass; FK violation closed. ✓ |
| Cascade list completeness vs FK references | All 25 distinct FK-targeting tables in db.py are in the cascade list. (`learning_insights` is in the cascade list but has no FK to research_tasks — pre-existing dead entry.) ✓ |
| DELETE investigation authorization / IDOR | `metadata->>'user_id' != current_user["user_id"]` gate at line 3565-3566 with admin override. Pre-Z-01 logic unchanged. ✓ |
| Daemon mid-settle race (user DELETE while daemon settling) | **AA-01 (P2)** — see findings. Cascade DELETE wipes research_settlements; parent DELETE wipes research_tasks; daemon's later `_claim_research_settlement` INSERT FK-violates because parent is gone; exception is swallowed; **reservation refund is permanently lost**. |
| auth.users cascade-delete | research_tasks.user_id ON DELETE CASCADE → research_tasks deletion → still blocked by research_settlements RESTRICT FK. Documented mitigation: operator-driven user-facing investigation deletes BEFORE auth.users delete. Unchanged from before Z-01. NONE. |

---

## Section 2 — Z-02 fix probe

### Methodology

1. Read `mariana/api.py:create_checkout` Z-02 derivation block (lines 5535-5553).
2. Read `tests/test_z02_stripe_redirect_allowlist.py` (4 tests).
3. Manually traced edge-case URL strings through `urlparse(...).hostname`:
   - `https://app.mariana.computer/return` → `app.mariana.computer` ✓ (in allowlist)
   - `https://app.mariana.computer.attacker.com/` → `app.mariana.computer.attacker.com` (NOT in allowlist) → 400 ✓
   - `https://APP.MARIANA.COMPUTER/` → `app.mariana.computer` (urlparse lowercases) ✓
   - `//app.mariana.computer/path` (scheme-less) → `app.mariana.computer` (allowed; Stripe rejects scheme-less anyway, not a bypass)
   - Trailing slash, IDN punycode, userinfo prefix — all yield the bare hostname; allowlist match is exact equality, not substring.
4. Verified `_DEFAULT_PROD_CORS_ORIGINS` (api.py:450) has `https://app.mariana.computer` and `https://frontend-tau-navy-80.vercel.app`; `_DEFAULT_DEV_CORS_ORIGINS` has `http://localhost:5173` and `http://localhost:3000`. Plus explicit `localhost`/`127.0.0.1` retention for ports the CORS list does not enumerate.

### Findings — Z-02

NONE. The derivation is module-load-time evaluated at function call (not import time, so `_DEFAULT_PROD_CORS_ORIGINS` reflects the current module state), uses exact-equality hostname check, and is closed-set so open-redirect protection is preserved.

---

## Section 3 — New-surface sweep

| # | Surface | Probed | Result |
|---|---------|--------|--------|
| 1 | DELETE investigation authorization / IDOR | Owner check via `metadata->>'user_id'` + admin override at lines 3562-3566. UUID validated upstream via `_validate_task_id` (BUG-API-001). | NONE |
| 2 | Cascade list completeness | 25 FK targets all covered by the 26-entry cascade list. `learning_insights` is dead-but-harmless (no task_id column; silent catch). | NONE |
| 3 | CORS dynamic vs module-load-time | `_DEFAULT_PROD_CORS_ORIGINS` is module-level. The `_ALLOWED_REDIRECT_HOSTS` derivation at line 5542 runs on EVERY checkout call (inside the function body), not at import time. So if an operator hot-patches `_DEFAULT_PROD_CORS_ORIGINS` between requests, the new value is picked up. | NONE |
| 4 | Stripe customer-portal session replay | `billing_portal.Session.create(customer=stripe_customer_id)` where customer_id is fetched server-side via `_get_stripe_customer_id(user_id)`. User cannot inject another user's customer_id. | NONE |
| 5 | ALTER ADD COLUMN performance on large tables | `ALTER TABLE research_tasks ADD COLUMN IF NOT EXISTS credits_settled BOOLEAN NOT NULL DEFAULT FALSE` is metadata-only on Postgres 11+ (no table rewrite). Safe. | NONE |
| 6 | GDPR / right-to-erasure | Mariana has no `DELETE /api/users/me` endpoint. User-deletion is delegated to Supabase. | NONE (out of scope) |
| 7 | Reconciler-vs-reconciler concurrency for research_settlements | `UPDATE research_settlements SET claimed_at = now() WHERE task_id IN (SELECT ... FOR UPDATE SKIP LOCKED)` — same pattern as T-01. Concurrent reconcilers see disjoint candidate sets. | NONE |
| 8 | Other UPDATE paths on research_settlements without lock | All four mutation sites (`_claim_research_settlement` INSERT, `_mark_research_ledger_applied`, `_mark_research_settlement_completed`, reconciler bump) use idempotent `WHERE … IS NULL` filters. No unguarded UPDATE. | NONE |
| 9 | Test/dev backdoors | `grep DEBUG\|TEST_MODE\|test@\|admin@\|impersonate\|act_as` — only `DEBUG` controls CORS dev origins; no auth backdoor. | NONE |

---

## Section 4 — Findings

| Bug ID | Priority | File:line | Evidence | Suggested fix |
|--------|----------|-----------|----------|---------------|
| **AA-01** | **P2** | `mariana/main.py:_deduct_user_credits` (lines 489-752) interacting with `mariana/api.py:delete_investigation` (lines 3589-3644) | When a user clicks DELETE on a RUNNING investigation, the API at line 3577-3581 sets `status='FAILED'` + publishes Redis kill, then cascades through child tables (line 3621-3634), then DELETEs the parent row (line 3639-3642). The orchestrator detects the kill within its poll cycle (seconds), exits, and `_run_single` calls `_deduct_user_credits(task_id=task.id, db=db)`. By that time, the API may have already wiped both `research_settlements` and `research_tasks`. The settlement helper at line 580-597 calls `_claim_research_settlement` which does `INSERT INTO research_settlements (task_id, …)`. The FK `research_settlements.task_id REFERENCES research_tasks(id) ON DELETE RESTRICT` (still in place — Z-01 only added cascade on the API side, not bypass) raises `ForeignKeyViolationError`. The `except Exception` at line 590 catches and returns. **The user's reservation refund is silently lost** — the keyed `grant_credits` RPC is never issued. **Concrete repro:** (1) user submits investigation → reserves R credits (deducted from balance). (2) immediately calls DELETE → API wipes both rows. (3) daemon mid-flight settles, claim INSERT FK-violates, settle returns without refund. User loses R credits with no investigation work delivered. Not exploitable across users (DELETE is owner-scoped) but a real direct-financial-loss path the user can trip by clicking DELETE on a running task at the wrong moment. | The settlement helper should fall through to issue the keyed ledger RPC even when the claim INSERT fails because the parent row is gone. The live `grant_credits` / `refund_credits` are idempotent on `(ref_type, ref_id)` against `credit_transactions` so a worst-case replay returns `status='duplicate'`. Concretely: catch `ForeignKeyViolationError` (or `asyncpg.exceptions.ForeignKeyViolationError`) inside the `_claim_research_settlement` try/except, detect the "parent gone" case, and continue to the RPC issuance branch (with the synthetic `_ref_id = task_id` flowing the same idempotency key). After RPC succeeds, skip the marker UPDATEs (they would no-op on missing row anyway) and log `credit_settlement_orphan_refund_ok` for operator visibility. Add a regression test in `tests/test_aa01_orphan_settlement_refund.py` that (a) inserts research_tasks + reservation, (b) wipes research_tasks (simulate user DELETE), (c) calls `_deduct_user_credits` with the orphan task_id, (d) asserts the keyed RPC was issued exactly once, (e) asserts the ledger mutation lands. |

---

## Section 5 — Verdict

**ONE FINDING.** Streak resets to **0 / 3**.

- **AA-01 (P2)** — Y-01/Z-01 interaction creates a daemon-mid-settle reservation-loss path. When a user clicks DELETE on a RUNNING task, the cascade DELETE wipes the parent research_tasks row before the daemon can issue the settlement claim INSERT. The FK violation is silently swallowed and the user's reservation refund is permanently lost. Concrete reproducer described above.
- Z-01 fix itself: clean. The FK violation it targeted is closed for the steady-state case (delete a SETTLED task). The race window for delete-while-settling is the residual surface this audit caught.
- Z-02 fix: clean. Allowlist derivation is correct and resilient to edge-case URL forms. Open-redirect protection holds.

The streak does NOT close. Fix is minimal: catch the FK-violation in `_claim_research_settlement` and fall through to the idempotent keyed RPC.
