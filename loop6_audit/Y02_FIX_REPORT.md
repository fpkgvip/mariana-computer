# Y-02 Fix Report — migration 022 missing revert script

Status: **FIXED 2026-04-28**
Severity: P4 (operational rollback gap; no runtime correctness impact)
Branch: `loop6/zero-bug`

## Bug

Phase E re-audit #26 (A31) noted that migration
`frontend/supabase/migrations/022_u01_stripe_pending_reversals.sql`
shipped without a paired `022_revert.sql`. Migrations 004 through 021
each have a paired revert script; 022 broke the convention. A
production rollback of U-01 would require operator-authored ad-hoc SQL.

## Fix

Added `frontend/supabase/migrations/022_revert.sql` matching the style
of `021_revert.sql`:

```sql
-- Revert migration 022 — U-01 stripe_pending_reversals out-of-order parking lot.

BEGIN;

DROP INDEX IF EXISTS public.idx_stripe_pending_reversals_pi_unapplied;
DROP INDEX IF EXISTS public.idx_stripe_pending_reversals_charge_unapplied;
DROP TABLE IF EXISTS public.stripe_pending_reversals;

COMMIT;
```

Index names verified against migration 022 (`grep CREATE INDEX
022_u01_stripe_pending_reversals.sql`). All three statements use IF
EXISTS so the revert is itself idempotent.

## Out of scope

No code or test changes — this is a missing operational artefact. The
existing 022 migration is unchanged.
