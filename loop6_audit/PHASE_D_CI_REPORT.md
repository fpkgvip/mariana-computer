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
| `frontend-lint` | yes | eslint via `npm run lint`. The 13 pre-existing react-hooks errors flagged at CI rollout were remediated in the same Phase D follow-up that promoted this job from advisory to required. |
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


---

## Coverage Fill (Phase D follow-up)

After the CI gates landed, the loop6 audit grew the backend baseline by
**+37 net tests** (38 added, 1 latent production bug fixed) across three
high-blast-radius surfaces: the agent loop, the settlement reconciler,
and the vault encryption / RLS layer.  Each surface ships in its own
commit so a regression bisect can pin the *area* before drilling into
individual cases.

### Commit chain

| Commit | What | Net |
| --- | --- | --- |
| `159ee57` | Phase D coverage: agent loop behavioural tests (+8) | +8 |
| `c85bd0f` | **CC-02**: settlement reconciler `batch_size` LIMIT silently ignored | (bug fix, prod) |
| `35e8dd8` | Phase D coverage: settlement reconciler edge case tests (+6) | +6 |
| `45a28c5` | Phase D coverage: vault encryption + RLS tests (+23) | +23 |

`tests/test_cc01_agent_loop_behavioural.py` (8 cases) pins:

* planner failure → FAILED + `planner_failed:` error prefix;
* pre-plan stop short-circuit → HALTED with zero planner spend;
* unexpected exception in `_run_one_step` is caught and the step is
  marked FAILED rather than crashing the loop;
* Redis `get` failure during `_check_stop_requested` is treated as
  "no stop" (resilience invariant);
* `_budget_exceeded` returns `budget_exhausted:` and `duration_exhausted:`
  on the spend and wallclock branches respectively;
* `_attempt_replan` returns False once `replan_count == max_replans`;
* hard-cap clamping (`_HARD_MAX_REPLANS`, `_HARD_MAX_FIX_PER_STEP`) at
  `run_agent_task` entry — a malicious caller cannot weaken defences;
* `requires_vault=True` with `redis=None` fails CLOSED before the
  planner runs.

`tests/test_cc02_settlement_reconciler_edge_cases.py` (6 cases) pins:

* T-01 marker-fixup short-circuit: `ledger_applied_at IS NOT NULL` →
  no RPC re-issued, only `completed_at` stamped;
* claim younger than `max_age_seconds` is invisible to the reconciler;
* empty candidate set → returns 0 with no settle attempts;
* `batch_size` LIMIT is honoured (this is the test that revealed the
  CC-02 production bug — see below);
* loader returning `None` for one row is logged and skipped without
  aborting the batch;
* a per-row exception in `_mark_settlement_completed` is logged and
  swallowed; subsequent rows still process.

The second item — `batch_size` LIMIT — surfaced a real production bug.
The reconciler's candidate query used `WHERE task_id IN (SELECT ... LIMIT
$2 ... FOR UPDATE SKIP LOCKED)`.  PostgreSQL is free to inline that
subquery as a semi-join, in which case the `LIMIT` applies to the join
output rather than to the candidate set; the outer UPDATE then matches
every uncompleted row.  Wrapping the candidate query in a CTE forces
materialisation and makes `LIMIT` operate on the candidate set as
intended.  See `loop6_audit/CC02_RECONCILER_LIMIT_FIX.md` for the full
diagnosis.  The fix was committed BEFORE the test that revealed it, in
keeping with Loop 6's "fix the bug first, then pin it" rule.  Both
agent-side and research-side reconcilers were patched identically.

`tests/test_cc03_vault_encryption_rls.py` (23 cases, 12 of which are
parametrised name-grammar checks) pins the pure-Python crypto-byte
invariants in `mariana/vault/store.py` and the RLS defence-in-depth
contract:

* `_validate_lengths` rejects short blob (< 16 B / GCM tag size),
  oversize blob (> blob_max), wrong salt size (≠ 16), and wrong IV size
  (≠ 12);
* `create_secret` refuses oversize blobs *before* any HTTP request fires
  (no DDoS of Supabase from a buggy client);
* `_validate_name` rejects shell metacharacters (`;`, `$`, backtick,
  `|`, `&`, `>`, `/`, whitespace) and lower-case starters; accepts the
  canonical `^[A-Z][A-Z0-9_]{0,63}$` grammar;
* `create_secret`, `update_secret`, `delete_secret`, and `get_vault`
  all carry the appropriate `user_id=eq.<uid>` filter and (for
  per-secret mutations) the redundant `id=eq.<sid>` filter so a
  leaked secret_id cannot be cross-user weaponised;
* `_from_bytea` rejects malformed hex / non-string inputs / garbage
  base64 with `VaultError` rather than returning silent empty bytes.

### Numbers

| Metric | Before | After | Delta |
| --- | --- | --- | --- |
| pytest passing | 406 | 443 | **+37** |
| pytest skipped | 13 | 13 | 0 |
| pytest failing | 0 | 0 | 0 |
| Production bugs found | — | 1 (CC-02) | — |

Test discovery time on the local Postgres baseline: 7.54 s.  No new
flaky time-based assertions were introduced (the only `time` use is to
anchor `_budget_exceeded` to "now" with `time.time()` so the wallclock
branch does not falsely trip).

### Out of scope (deferred)

* End-to-end RLS tests against a live Supabase project — `auth.users`
  isn't in `testdb`, so the existing `tests/test_vault_integration.py`
  and `tests/test_vault_live.py` retain the live coverage; the CC-03
  suite covers the library-layer contract that complements them.
* Property-based tests for byte-length boundaries (Hypothesis is not in
  `requirements.txt`).
* Mutation testing — still out of scope.
