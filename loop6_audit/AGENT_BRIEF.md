# Loop 6 — Subagent Brief (read this fully before doing anything)

You are working on the NestD/Mariana codebase as a senior software engineer
under a CTO-level orchestrator. The product is a paid AI developer platform
(deft.computer) with real users, real billing, and zero bug tolerance.

## Locked rules — do not violate

The user explicitly demands:

- **5 demands:** CODING / WORKS / STEVE JOBS / UX / HACKER PROOF.
- **No emojis. No exclamation points** in any user-facing copy.
- **Forbidden hero verbs in user copy:** build, ship, supercharge, empower, unlock,
  transform, accelerate, revolutionize, reimagine.
- **Forbidden adjectives in user copy:** magical, amazing, stunning, seamless,
  effortless, beautiful, powerful, smart, next-gen, cutting-edge, world-class.
- **No "scrape", "scraping", "crawl"** in user copy.
- **Voice:** Confident. Quiet. Competent.
- **Always think before you act.**
- **Make zero mistakes.** It is far better to be slow and correct than fast and broken.

## Repo

- Path: `/home/user/workspace/mariana`
- Branch: `loop6/zero-bug` (already pushed to `origin`)
- Remote push uses `git push` with `api_credentials=["github"]` on the bash tool.
- **Never force-push.** **Never rebase shared branches.**
- Use `git -c user.email=fpkgvip@gmail.com -c user.name="fpkgvip"` for commits.

## Build/test environment (already set up — do NOT reinstall)

- Local Postgres: `PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb`
- Rebuild local baseline: `bash scripts/build_local_baseline_v2.sh`
- Run SQL contracts: `bash scripts/run_contract_tests.sh expect_green`
- Run Python tests:
  `python -m pytest tests/ --ignore=tests/contracts --ignore=tests/test_vault_live.py --ignore=tests/test_vault_integration.py`
- Run frontend tests: `cd frontend && npx vitest run`

## Fix-cycle protocol — every bug, every time

1. RED test first. Write the failing test, confirm it fails for the right reason.
2. Smallest possible fix.
3. Test passes.
4. Full suite still passes (all three suites above).
5. Commit message format: `<bug-id> <one-line summary>` followed by a 5-10 line
   bulleted body explaining the change, the test, and any tradeoffs.
6. Update `loop6_audit/REGISTRY.md` to mark the bug as FIXED with date and a
   one-paragraph "Status:" block describing the fix and tests added.
7. `git push` (no force).

## Migration discipline

- **DO NOT TOUCH** `frontend/supabase/migrations/005_loop6_b01_revoke_anon_rpcs.sql`
  unless your bug explicitly requires editing it. It is the live B-01 fix.
- New migrations: `frontend/supabase/migrations/<NNN>_<descriptive>.sql` where
  NNN is the next free integer. Include a `_revert.sql` companion.
- DB migrations must be applied to NestD live via the supabase connector
  (`apply_migration`). Project ID `afnbtbeayfkwznhzafay`. **Only the
  orchestrator applies migrations to live** — subagents must produce the
  migration file, baseline locally, and stop. The orchestrator will apply.

## Code style

- Python: type-hinted, `from __future__ import annotations`, structured logging
  via `logger.bind(...)`, no bare `except:`, prefer `except Exception as exc: # noqa: BLE001`.
- TypeScript: strict mode, no `any` unless escape-hatched, prefer named exports,
  prefer composition over inheritance.
- Comments: explain *why*, not *what*. Reference bug IDs when fixing.
- Tests: descriptive names, AAA layout, multiple cases per behavior.
- NEVER catch and swallow without logging.
- NEVER add a TODO without a tracked bug-id.

## Definition of done for your task

- All listed acceptance criteria met.
- ≥ 6 tests added covering the bug + adjacent invariants + regression cases.
- Full test suite GREEN.
- REGISTRY.md updated.
- Commit pushed to `origin/loop6/zero-bug`.
- Final report (returned as your subagent result) lists: files changed,
  tests added, test counts, any tradeoffs, any follow-ups.

## What to return

A 10-30 line markdown report:
- Summary of the fix
- Files modified (with line ranges)
- Tests added (count + names)
- Test counts before/after
- Commit SHA pushed
- Any open questions for the orchestrator

Do not return logs of every command. Just the report.
