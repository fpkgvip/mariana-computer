# CC-17 Fix Report — pin `search_path` on every historical and revert SECURITY DEFINER function

**Date:** 2026-04-28
**Audit source:** `loop6_audit/A47_deep_sweep_reaudit.md` — finding CC-17, P2
**Branch:** `loop6/zero-bug`

---

## Finding (recap)

Three migration files defined or redefined `SECURITY DEFINER` functions WITHOUT a
`SET search_path = …` clause:

- `frontend/supabase/migrations/001_initial_schema.sql:55-63`
- `frontend/supabase/migrations/004_revert.sql:55-93`
- `frontend/supabase/migrations/007_revert.sql:17-124`

A `SECURITY DEFINER` function with an unpinned `search_path` runs with the
caller's `search_path` and the function-owner's privileges. An attacker who can
create objects in any schema on the resolution list (e.g. their own schema, or
a public-writable schema) can shadow `public.profiles` / `public.is_admin` etc.
with a malicious object and execute arbitrary code as `postgres`.

The live production database is already hardened — migration `007 forward` and
the `B-02` hardening pass added `SET search_path = public, pg_temp` to every
live SECURITY DEFINER function. The CI baseline `.github/scripts/ci_full_baseline.sql`
(a `pg_dump` of live state) confirms this on every SD function with the
`SET search_path TO 'public', 'pg_temp'` clause.

The risk is therefore not in the steady state but in **rollback** and
**fresh-baseline rebuild** paths: running `*_revert.sql` or replaying `001`
re-creates the unpinned definitions and re-opens the attack surface.

---

## Sweep result

A pure-Python parser (`tests/test_cc17_security_definer_search_path.py`)
identified **82 SECURITY DEFINER function definitions** across all migration
files plus `ci_full_baseline.sql`. Of those, **12 were missing**
`SET search_path` — all confined to the three files above. **0 missing** in
`ci_full_baseline.sql`, confirming **no production drift**.

---

## Functions modified (file:line, function name, fix)

All fixes add `SET search_path = public, pg_temp` to the function definition.
Function logic / signatures / return types / language are unchanged.

| # | File | Line(s) | Function | Where the SET clause was inserted |
|---|------|---------|----------|------------------------------------|
| 1 | `frontend/supabase/migrations/001_initial_schema.sql` | 63 | `public.handle_new_user()` | post-body attribute clause: `$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp;` |
| 2 | `frontend/supabase/migrations/004_revert.sql` | 62-63 | `public.admin_set_credits(uuid, integer, boolean)` | pre-body header clause, between `SECURITY DEFINER` and `AS $function$` |
| 3 | `frontend/supabase/migrations/007_revert.sql` | 18 | `public.add_credits(uuid, integer)` | pre-body header clause, between `SECURITY DEFINER` and `AS $$` |
| 4 | `frontend/supabase/migrations/007_revert.sql` | 27 | `public.admin_count_profiles()` | pre-body header clause |
| 5 | `frontend/supabase/migrations/007_revert.sql` | 36 | `public.admin_list_profiles()` | pre-body header clause |
| 6 | `frontend/supabase/migrations/007_revert.sql` | 45 | `public.check_balance(uuid)` | pre-body header clause |
| 7 | `frontend/supabase/migrations/007_revert.sql` | 50 | `public.deduct_credits(uuid, integer)` | pre-body header clause |
| 8 | `frontend/supabase/migrations/007_revert.sql` | 66 | `public.get_stripe_customer_id(uuid)` | pre-body header clause |
| 9 | `frontend/supabase/migrations/007_revert.sql` | 75 | `public.get_user_tokens(uuid)` | pre-body header clause |
| 10 | `frontend/supabase/migrations/007_revert.sql` | 84 | `public.handle_new_user()` | pre-body header clause |
| 11 | `frontend/supabase/migrations/007_revert.sql` | 93 | `public.update_profile_by_id(uuid, jsonb)` | pre-body header clause |
| 12 | `frontend/supabase/migrations/007_revert.sql` | 112 | `public.update_profile_by_stripe_customer(text, jsonb)` | pre-body header clause |

**Total functions modified: 12**

---

## Choice of `public, pg_temp`

PostgreSQL recommends restricting `search_path` to schemas the function actually
needs.  All 12 functions reference user-defined objects (`public.profiles`,
`public.credit_*`, `public.admin_audit_insert`, `auth.uid()`), so they need
`public`.  `pg_temp` is added per Supabase / PostgreSQL convention so any
private temp objects the function might materialise resolve to its own session.
Built-in `pg_catalog` is implicitly searched first regardless of `search_path`,
so adding it explicitly is unnecessary.

This is the same form already used by `ci_full_baseline.sql` (the live-DB
pg_dump) and by every forward migration in this codebase (`002`, `004`,
`007 forward`, `008+`, `011`, `012`, `015`, `018`, `021`, `024`).

---

## Verification

### Static parser (regression test)

`tests/test_cc17_security_definer_search_path.py` runs in pure Python with no
DB dependency. It parses every `.sql` file under
`frontend/supabase/migrations/` plus `.github/scripts/ci_full_baseline.sql`,
extracts every `CREATE [OR REPLACE] FUNCTION ... SECURITY DEFINER` block
(including any post-body attribute clause), and asserts each block contains a
`SET search_path = …` or `SET search_path TO …` clause.

```
$ python3 -m pytest tests/test_cc17_security_definer_search_path.py -v
tests/test_cc17_security_definer_search_path.py::test_every_security_definer_function_pins_search_path PASSED
tests/test_cc17_security_definer_search_path.py::test_parser_finds_known_function PASSED
2 passed in 0.04s
```

### Local baseline rebuild

```
$ PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb \
    bash scripts/build_local_baseline_v2.sh
… (truncated)
Verifying critical RPCs are present...
Local baseline v2 ready (mirrors CI baseline).
```

### Full pytest suite

With **only** the CC-17 changes applied (other agents' parallel WIP for CC-18 /
CC-19 / CC-21 stashed for clean isolation):

```
513 passed, 11 skipped, 11 warnings in 7.92s
```

Up from the pre-CC-17 baseline of 511 passed (the 2 new tests are the two
checks in the CC-17 regression test).

The pre-existing `tests/test_z01_research_delete_cascade.py` and
`tests/test_z02_stripe_redirect_allowlist.py` failures observed under the
parallel-agent WIP are unrelated to this change — they pass cleanly when only
CC-17 changes are present, confirming no CC-17-induced regression.

---

## CI baseline drift check

Full sweep of `.github/scripts/ci_full_baseline.sql`: every SECURITY DEFINER
function definition (20 occurrences) carries
`SET search_path TO 'public', 'pg_temp'`. **No drift.** The pg_dump matches
the live hardened state.

---

## Files changed

- `frontend/supabase/migrations/001_initial_schema.sql` (1 SD function pinned)
- `frontend/supabase/migrations/004_revert.sql` (1 SD function pinned)
- `frontend/supabase/migrations/007_revert.sql` (10 SD functions pinned)
- `tests/test_cc17_security_definer_search_path.py` (new regression test)
- `loop6_audit/REGISTRY.md` (CC-17 row added)
- `loop6_audit/CC17_FIX_REPORT.md` (this file)
