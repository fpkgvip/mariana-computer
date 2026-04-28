# Phase D — GitHub Actions CI Setup

Status: **LANDED 2026-04-28**
Branch: `loop6/zero-bug`
Workflow: `.github/workflows/ci.yml`

## Why this exists

Loop 6 closed at 3/3 zero-finding rounds across 34 adversarial audits and
~90 fixes. Every fix landed a regression test. Phase D wires those tests
into a CI gate so the Loop 6 invariants hold on every future PR.

The workflow runs on:

* Every pull request.
* Direct push to `main`, `master`, or `loop6/zero-bug`.

Concurrency is keyed on `github.ref` with `cancel-in-progress: true`, so
a second push to the same PR cancels the in-flight run.

## Jobs

| Job | Required? | What it covers |
|-----|-----------|----------------|
| `backend-tests` | yes | pytest against postgres:16 + redis:7 service containers, with the full schema baseline pre-applied. Baseline 406 passing / 13 skipped / 0 failed. |
| `frontend-tests` | yes | vitest against `frontend/`. Baseline 144 passing. |
| `frontend-lint` | advisory | eslint via `npm run lint`. Marked `continue-on-error: true` because the repo currently carries 13 pre-existing react-hooks errors that pre-date this CI setup. The job still surfaces new lint regressions in the run summary; once the existing errors are remediated, flip the gate to required by removing `continue-on-error`. |
| `frontend-typecheck` | yes | `npx tsc --noEmit -p tsconfig.json`. Currently passes clean. |
| `frontend-build` | yes | `npm run build` (vite production bundle). |
| `sql-lint` | yes | `.github/scripts/check_migration_pairs.sh` — every forward migration `NNN_*.sql` (revision >= 004) must have a paired `NNN_revert.sql` (or `NNN_<name>_revert.sql`). Y-02 convention. |
| `security-checks` | yes | (a) secret scan via `.github/scripts/check_secrets.sh` — high-confidence prefixes only (`sk_live_`, `pk_live_`, `whsec_<32+>`, `AKIA[A-Z0-9]{16}`, `xox[pboa]-...`, `gh[pousr]_...`); excludes `loop6_audit/`, `tests/`, frontend test dirs and lockfiles. (b) `npm audit --omit=dev --audit-level=high`. |
| `registry-integrity` | yes | `.github/scripts/check_registry_integrity.sh` — fails if any row in `loop6_audit/REGISTRY.md` is in the `**OPEN**` state. |

All jobs except `frontend-lint` are eligible for GitHub branch-protection
required-status-checks.

## Schema baseline (CI-only)

Stock `postgres:16` does not have Supabase's auth schema, the platform
roles (`anon`, `authenticated`, `service_role`), or the columns added
to `profiles` through Supabase's web UI rather than committed
migrations. Iterating `frontend/supabase/migrations/*.sql` against a
clean DB therefore fails (verified locally — migration 002 references
`profiles.role` which is not added by 001).

Phase D ships two CI-only artefacts:

* `.github/scripts/ci_pg_bootstrap.sql` — creates the `anon` /
  `authenticated` / `service_role` roles and installs `pgcrypto`.
  Runs FIRST.
* `.github/scripts/ci_full_baseline.sql` — full `pg_dump -s
  --no-owner --no-acl --schema=public --schema=auth` of the local
  testdb after every migration through 024 has been applied. 3354
  lines. `CREATE SCHEMA` lines were post-processed to `CREATE SCHEMA
  IF NOT EXISTS` so the role-bootstrap can run before the baseline.
  PG-17 `\restrict` / `\unrestrict` directives stripped (CI uses PG
  16).

`apply_migrations.sh` wires the two together and verifies seven
critical RPCs are present after the baseline applies
(`grant_credits`, `refund_credits`, `spend_credits`, `expire_credits`,
`add_credits`, `process_charge_reversal`, `admin_set_credits`).

### How to refresh the baseline after a new migration

When a new migration `NNN_*.sql` lands:

1. Apply it locally: `psql -f frontend/supabase/migrations/NNN_*.sql`
   against the local testdb.
2. Re-dump:
   ```
   PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb \
     pg_dump -s --no-owner --no-acl --schema=public --schema=auth \
     > .github/scripts/ci_full_baseline.sql
   sed -i '/^\\\(restrict\|unrestrict\)/d' .github/scripts/ci_full_baseline.sql
   sed -i 's/^CREATE SCHEMA public;$/CREATE SCHEMA IF NOT EXISTS public;/' .github/scripts/ci_full_baseline.sql
   sed -i 's/^CREATE SCHEMA auth;$/CREATE SCHEMA IF NOT EXISTS auth;/' .github/scripts/ci_full_baseline.sql
   ```
3. Commit the regenerated baseline alongside the migration.

Future-proofing: the long-term path is to switch the CI Postgres to a
Supabase-emulating image that ships the full auth schema natively
(e.g. `supabase/postgres` or running `supabase start` as a service in
the workflow). For now, the dumped baseline is the simplest gate
that actually runs the Loop 6 regression tests as written.

## Adding new tests

* **New pytest** — drop a `tests/test_<id>_<name>.py` file. The
  `backend-tests` job picks it up automatically. If the test exercises
  a new SQL function, ensure (a) the function lives in a committed
  migration, (b) the baseline is refreshed per the procedure above.
* **New vitest** — drop a `*.test.ts` or `*.test.tsx` file under
  `frontend/src/`. The `frontend-tests` job picks it up automatically.
* **New SQL migration** — add `NNN_<name>.sql` AND `NNN_revert.sql`
  (or `NNN_<name>_revert.sql`). The `sql-lint` job enforces the pair.

## Convergence invariant

The `registry-integrity` job is the durable encoding of the Loop 6
3-of-3 zero-finding outcome. While `loop6_audit/REGISTRY.md` has zero
`**OPEN**` rows, every PR must keep it that way: any new `**OPEN**`
row added without a paired `**FIXED**` flip blocks the merge. This
forces the discipline of "find a defect → ship the fix in the same PR
that opens it" that produced convergence in the first place.

## Locally reproducing CI

The check scripts are the same ones CI runs:

```
bash .github/scripts/check_migration_pairs.sh
bash .github/scripts/check_registry_integrity.sh
bash .github/scripts/check_secrets.sh
```

For the backend-tests path, replicate the CI baseline:

```
PGHOST=/tmp PGPORT=55432 PGUSER=postgres psql -d postgres -c \
  'DROP DATABASE IF EXISTS testdb_ci; CREATE DATABASE testdb_ci'
PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb_ci \
  bash .github/scripts/apply_migrations.sh
PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb_ci \
  python -m pytest -x --tb=short
```

For the frontend, run from `frontend/`:

```
npm ci
npm test
npm run lint
npx tsc --noEmit -p tsconfig.json
npm run build
```

## Out of scope

* Python lint (`ruff` / `black` / `mypy`) — repo does not currently
  configure any of these; introducing them in this PR would expand
  scope. A follow-up should pick one and add a config + CI job.
* Coverage reporting — pytest-cov is not in `requirements.txt`. A
  follow-up could pin a coverage tool and an artefact upload.
* Performance / load tests — out of scope for Loop 6 zero-bug
  convergence.
* Mutation testing — out of scope.
