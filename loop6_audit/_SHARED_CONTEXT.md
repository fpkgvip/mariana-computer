# Loop 6 — Shared Audit Context (read by every audit subagent)

## Mission

Find every bug in the Mariana / NestD system that prior audits missed. The
prior 16 verification rounds claimed "PASS — 0 vulnerabilities" but missed:
R1 (two PERMISSIVE UPDATE policies on profiles), R2 (Stripe replay via narrow
idempotency idx), R5 (admin_set_credits no search_path / no audit), R3+R6
(10M-token ledger drift). This time we hunt with five lenses in parallel.

Read this entire file before doing anything.

## Repo geography

- **Branch:** `loop6/zero-bug` (off `loop5/phase0-rebuild`)
- **Repo root:** `/home/user/workspace/mariana`
- Python backend: `mariana/` (api.py = 7890 lines, 88 routes)
- Frontend: `frontend/src/` (160 .ts/.tsx files, React + Vite + Tailwind)
- Migrations: `frontend/supabase/migrations/` (001-004 + 004b applied to live)
- Existing tests: `tests/contracts/` (7), `tests/tools/` (1), `tests/test_*.py` (6)
- Old verification docs: `verification-*.md` and `BUG_AUDIT.md` (root) — **prior agents missed bugs, treat as suspect, do not trust**

## Live Supabase project — NestD

- **Project ID:** `afnbtbeayfkwznhzafay`
- **Org ID:** `zfycdbsmfiocgngochlu`
- **Region:** ap-southeast-2
- **Status:** ACTIVE_HEALTHY
- **Tools to use:** `call_external_tool(source_id="supabase", tool_name="execute_sql", arguments={"project_id":"afnbtbeayfkwznhzafay","query":"…"})` — READ-ONLY queries only. Do not write.

## Live evidence already pulled (read these before re-querying)

In `loop6_audit/../loop5_research/`:
- `LIVE_STATE_AUDIT.md`        — narrative summary
- `live_tables.json`           — table list
- `live_columns.json`          — every column
- `live_indexes.json`          — every index
- `live_policies.json`         — every RLS policy
- `live_all_functions.json`    — every function (names only)
- `live_rpc_bodies.json`       — function bodies
- `live_admin_set_credits.json` — pre-fix snapshot (now stale; R5 already fixed)
- `live_audit_expire.json`     — expire_credits snapshot
- `live_credit_rpcs.json`      — credit-touching RPCs

NOTE: live snapshots are pre-Loop-5-fix. Cross-check against current live before reporting any bug. R1, R2, R5 are now FIXED in live. R3, R6 still open.

## Local Postgres (for reproductions)

- `PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb`
- `scripts/build_local_baseline.sh` — wipes/reseeds with live-faithful schema
- After applying 004 + 004b, the local testdb mirrors current live state
- `scripts/run_contract_tests.sh expect_red|expect_green` — runs contract suite

## Already-known issues (do NOT re-report)

| ID | Status      |
|----|-------------|
| R1 | FIXED in live (single UPDATE policy on profiles)                                  |
| R2 | FIXED in live (uq_credit_tx_idem covers grant + refund + expiry)                   |
| R3 | OPEN — profiles.tokens vs ledger drift; needs api.py work; reconcile_ledger.py exists |
| R4 | PARTIAL — admin_set_credits now uses is_admin; other admin RPCs still inconsistent |
| R5 | FIXED in live (admin_set_credits has search_path, writes audit_log)                |
| R6 | OPEN — add_credits / deduct_credits skip ledger; needs api.py work                  |
| R7 | DECIDED-OUT — expire_credits has advisory lock                                      |

R3, R4 (remaining), R6 are valid findings — keep reporting them with new evidence if you find related sub-bugs.

## Finding schema (every report uses this exact YAML)

```yaml
- id: <YOUR_LENS>-<auto-incr from 1>          # e.g. A1-01, A2-15
  severity: P0 | P1 | P2 | P3 | P4
  category: security | money | integrity | availability | correctness | performance | ux
  surface: db | api | orchestrator | frontend | cross
  title: <one line, no period at end>
  evidence:
    - file: <abs path or 'pg_catalog'>
      lines: <line range or query>
      excerpt: |
        <verbatim code or output that demonstrates the bug>
    - reproduction: |
        <steps to reproduce, including any commands>
  blast_radius: <one paragraph: who is affected, how often, what exposure>
  proposed_fix: |
    <one paragraph; reference specific files/lines if possible>
  fix_type: migration | api_patch | frontend_patch | config | test_only | docs
  test_to_add: |
    <name + the failure mode the test must catch>
  blocking: [<other finding ids that must land first, or 'none'>]
  confidence: high | medium | low
```

## Severity rubric (be strict)

- **P0** = active or trivially reachable security/financial/data-loss exposure. Drop everything.
- **P1** = guaranteed crash on common path, OR data corruption under realistic concurrency, OR money-leak under retry.
- **P2** = wrong business logic, edge-case crash, or auth-edge weakness.
- **P3** = perf regression, missing index, dropped log, dead code.
- **P4** = UX nit, copy issue, naming.

If unsure between two levels, report the higher one and note "could be downgraded if X."

## Anti-patterns prior audits used (DO NOT REPEAT)

1. **Reading migrations instead of live.** Always cross-check pg_catalog.
2. **Single mental model.** Diversify your reasoning — assume one lens missed the bug.
3. **Prose-only conclusions.** Every finding must include either a query result, a line of code, or a reproduction.
4. **Trusting "PASS" verdicts.** Every prior audit was wrong about something. Be skeptical.
5. **Missing concurrency.** Many bugs only show under racing requests. If you see a sequence that reads-then-writes, ask "what if two ran at once?"
6. **Ignoring drift.** Migration says X, live shows Y, code expects Z — three-way mismatch is fertile.

## Hard rules for the audit

- **READ ONLY.** Do not write to NestD live. Do not run `apply_migration`. Do not modify code on disk except your own findings file.
- **No `confirm_action` calls.** This is internal audit work, not user-facing.
- **Save findings to your assigned file.** Do not write anywhere else.
- **Stop conditions:** when you've covered your scope or burned ~30 minutes. If you hit a dead end on one item, document it and move on.

## Output file (per agent)

| Lens | File |
|---|---|
| A1 | loop6_audit/A1_db.md |
| A2 | loop6_audit/A2_api.md |
| A3 | loop6_audit/A3_orchestrator.md |
| A4 | loop6_audit/A4_frontend.md |
| A5 | loop6_audit/A5_adversarial.md |

Each file starts with a 5-line summary (counts by severity), then the YAML findings.
