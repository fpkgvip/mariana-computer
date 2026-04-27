# P2 DB Cluster B-11..B-15 — Fix Report

**Date:** 2026-04-27  
**Branch:** loop6/zero-bug  
**Migration:** `frontend/supabase/migrations/011_p2_db_cluster_b11_b15.sql`  
**Revert:** `frontend/supabase/migrations/011_revert.sql`  
**Applied to:** Local testdb (PGHOST=/tmp:55432) + NestD live (project `afnbtbeayfkwznhzafay`)

---

## Summary

Five P2 DB-tier findings from the Loop 6 audit were fixed in a single consolidated migration (011). All contract tests pass, full pytest suite passes (139 passed, 10 skipped), and all 51 frontend vitest tests pass.

---

## Bug-by-Bug Details

### B-11 — admin_count_profiles / admin_list_profiles inline auth check

**Root cause:** Both functions used `(SELECT role FROM public.profiles WHERE id = auth.uid()) = 'admin'` inline subquery instead of the canonical `public.is_admin(auth.uid())` helper.

**Fix:** Rewrote both functions as PL/pgSQL with:
```sql
IF NOT public.is_admin(auth.uid()) THEN
  RAISE EXCEPTION 'permission denied' USING ERRCODE = '42501';
END IF;
```
Both already had `SET search_path = public, pg_temp` from migration 007 (B-02 fix).

**Pre-fix state (live):** `prosrc` contained inline subquery with `= 'admin'` comparison  
**Post-fix state (live):** `prosrc` calls `public.is_admin(auth.uid())`

**Contract test:** `tests/contracts/C10_admin_helpers_use_is_admin.sql` — asserts body contains `is_admin` and does NOT contain the old inline pattern.

---

### B-12 — admin_audit_insert publicly executable

**Root cause:** `admin_audit_insert` had `proacl = {postgres=X, authenticated=X, service_role=X}` — authenticated users (and via the body check, any caller passing a valid admin UUID) could forge audit log entries.

**Fix:**
1. Rewrote function body to check `public.is_admin(auth.uid())` using the JWT-bound caller identity (not the passed-in `p_actor_id`)
2. Added actor_id mismatch check: `auth.uid() <> p_actor_id` raises 42501 — prevents replay attacks
3. Changed `search_path` from `public, auth` to `public, pg_temp` (removes auth schema from trusted path)
4. `REVOKE EXECUTE FROM anon, authenticated`; `GRANT EXECUTE TO service_role`

**C07 contract update:** Moved `admin_audit_insert` from group_b (authenticated keeps EXECUTE) to group_a (fully revoked). Updated array in `tests/contracts/C07_anon_rpc_deny.sql`.

**Pre-fix state (live):** `proacl = {postgres=X/postgres, authenticated=X/postgres, service_role=X/postgres}`  
**Post-fix state (live):** `proacl = {postgres=X/postgres, service_role=X/postgres}`

**Contract test:** `tests/contracts/C11_admin_audit_insert_anon_deny.sql`

---

### B-13 — expire_credits callable by anon

**Root cause:** On the local baseline (before migrations), `expire_credits` was granted to anon/authenticated. On live, migration 005 had already revoked this. The fix is idempotent.

**Fix:** `REVOKE EXECUTE FROM anon, authenticated; GRANT EXECUTE TO service_role` — idempotent on live, corrective on local baseline.

**Pre-fix state (live):** `proacl = {postgres=X/postgres, service_role=X/postgres}` (already clean)  
**Post-fix state (live):** Same — idempotent  
**Pre-fix state (local):** `proacl` included `anon` and `authenticated` from baseline GRANT before 005 migration

**Contract test:** `tests/contracts/C12_expire_credits_anon_deny.sql`

---

### B-14 — handle_new_user trigger failure leaves phantom auth.users

**Root cause:** The trigger function had no exception handling. If the profiles INSERT raised (CHECK violation, etc.), Supabase Auth might commit the `auth.users` row while the trigger rolled back, leaving a phantom identity with no profile.

**Fix — design decisions:**
1. **ON CONFLICT (id) DO NOTHING** on profiles INSERT — makes trigger idempotent; safe for retries
2. **Nested PL/pgSQL BEGIN/EXCEPTION block** for credit_buckets INSERT:
   - On bucket INSERT failure: `RAISE NOTICE` (logged) then `RAISE` (re-raises)
   - **Atomicity choice: ROLLBACK** — a user without a credit bucket is in an inconsistent state. Rolling back auth.users creation is safer than allowing a phantom user with no credits. The NOTICE is logged for observability.
3. **Outer EXCEPTION WHEN OTHERS** block re-raises any unexpected error from profiles INSERT — ensures auth.users INSERT rolls back (AFTER trigger, same transaction)
4. Added `SECURITY DEFINER + SET search_path = public, pg_temp` (was already SECURITY DEFINER but confirmed)
5. **credit_buckets INSERT added**: `(user_id, source='signup_grant', original_credits=500, remaining_credits=500)` — new users now get their initial 500 credits atomically at signup

**Pre-fix state (live):** Simple body, no exception handling, no credit_buckets INSERT  
**Post-fix state (live):** Full atomic trigger with nested exception handling

**Contract test:** `tests/contracts/C13_signup_trigger_atomic.py` — three tests:
- C13-A: Happy path creates profile + credit_bucket row
- C13-B: ON CONFLICT idempotency (replay doesn't error)
- C13-C: Bucket INSERT failure via temporary CHECK constraint → full rollback verified

---

### B-15 — credit_buckets / credit_transactions FK to auth.users

**Root cause:** `credit_buckets.user_id` and `credit_transactions.user_id` referenced `auth.users(id)` ON DELETE CASCADE. When a profile is deleted (which itself cascades from auth.users), ledger rows would not cascade properly. The indirection should be through `public.profiles`.

**Fix:**
1. **Orphan pre-check** (DO block): counts credit rows with no matching profiles row; aborts migration with RAISE EXCEPTION if any found. On live: 0 orphans confirmed.
2. DROP CONSTRAINT on both `credit_buckets_user_id_fkey` and `credit_transactions_user_id_fkey`
3. ADD CONSTRAINT referencing `public.profiles(id) ON DELETE CASCADE`

The chain is now: `auth.users → profiles (CASCADE) → credit_buckets (CASCADE) → credit_transactions (partial)`

**Pre-fix state (live):**
- `credit_buckets_user_id_fkey` → `auth.users(id)` ON DELETE CASCADE
- `credit_transactions_user_id_fkey` → `auth.users(id)` ON DELETE CASCADE

**Post-fix state (live):**
- `credit_buckets_user_id_fkey` → `profiles(id)` ON DELETE CASCADE
- `credit_transactions_user_id_fkey` → `profiles(id)` ON DELETE CASCADE

**Contract test:** `tests/contracts/C14_credit_tables_fk_to_profiles.sql`

---

## Test Results

### run_contract_tests.sh (SQL contracts)

```
MODE: expect_green
PASS: 11
FAIL: 0
  pass: C01 C02 C03 C04 C05 C06 C07 C10 C11 C12 C14 (GREEN)
  guard pass: G01
```

Note: C13 is a Python test run via pytest, not the SQL contract runner.

### pytest tests/

```
139 passed, 10 skipped in 4.74s
```

Includes C08 and C13 contract tests plus all existing test suites.

### npm run test (frontend)

```
Test Files  6 passed (6)
Tests  51 passed (51)
```

---

## Files Changed

| File | Action | Description |
|------|--------|-------------|
| `frontend/supabase/migrations/011_p2_db_cluster_b11_b15.sql` | Created | Main migration |
| `frontend/supabase/migrations/011_revert.sql` | Created | Revert migration |
| `tests/contracts/C10_admin_helpers_use_is_admin.sql` | Created | B-11 contract |
| `tests/contracts/C11_admin_audit_insert_anon_deny.sql` | Created | B-12 contract |
| `tests/contracts/C12_expire_credits_anon_deny.sql` | Created | B-13 contract |
| `tests/contracts/C13_signup_trigger_atomic.py` | Created | B-14 contract |
| `tests/contracts/C14_credit_tables_fk_to_profiles.sql` | Created | B-15 contract |
| `tests/contracts/C07_anon_rpc_deny.sql` | Modified | Moved admin_audit_insert from group_b to group_a |
| `scripts/build_local_baseline_v2.sh` | Modified | Added 011 to migration apply order |
| `loop6_audit/REGISTRY.md` | Modified | Marked B-11..B-15 FIXED in dedup table + DAG |

---

## Pre/Post Live State Snapshots

### Function ACL (proacl) — Pre-011

| Function | proacl |
|----------|--------|
| admin_audit_insert | `{postgres=X/postgres, authenticated=X/postgres, service_role=X/postgres}` |
| admin_count_profiles | `{postgres=X/postgres, authenticated=X/postgres, service_role=X/postgres}` |
| admin_list_profiles | `{postgres=X/postgres, authenticated=X/postgres, service_role=X/postgres}` |
| expire_credits | `{postgres=X/postgres, service_role=X/postgres}` |
| handle_new_user | `{postgres=X/postgres, service_role=X/postgres}` |

### Function ACL (proacl) — Post-011

| Function | proacl |
|----------|--------|
| admin_audit_insert | `{postgres=X/postgres, service_role=X/postgres}` ✓ |
| admin_count_profiles | `{postgres=X/postgres, authenticated=X/postgres, service_role=X/postgres}` (unchanged — admin-gated by body) |
| admin_list_profiles | `{postgres=X/postgres, authenticated=X/postgres, service_role=X/postgres}` (unchanged — admin-gated by body) |
| expire_credits | `{postgres=X/postgres, service_role=X/postgres}` ✓ |
| handle_new_user | `{postgres=X/postgres, service_role=X/postgres}` ✓ |

### FK Constraints — Pre-011

| Table | Constraint | References |
|-------|-----------|------------|
| credit_buckets | credit_buckets_user_id_fkey | auth.users(id) ON DELETE CASCADE |
| credit_transactions | credit_transactions_user_id_fkey | auth.users(id) ON DELETE CASCADE |

### FK Constraints — Post-011

| Table | Constraint | References |
|-------|-----------|------------|
| credit_buckets | credit_buckets_user_id_fkey | profiles(id) ON DELETE CASCADE ✓ |
| credit_transactions | credit_transactions_user_id_fkey | profiles(id) ON DELETE CASCADE ✓ |

---

## Guardrails Respected

- Did not modify `api.py` or any frontend code
- Did not modify any test files outside `tests/contracts/`
- Preserved all existing B-01 through B-09 + F-03/F-04 hardening
- Did not commit or push to git
- Other subagents' work (migrations 012+) unaffected
