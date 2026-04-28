# CC-05 Fix Report — reconciler `batch_size` validation

**Severity:** P3
**Audit source:** `loop6_audit/A40_phase_d_post_cc02_reaudit.md`
**Fix commit:** `7ea5465a03b0281457cdaa5306fe7934b9d344bf` on `loop6/zero-bug`
**Date:** 2026-04-28
**Author:** fpkgvip

---

## Bug

`_SETTLEMENT_RECONCILE_BATCH_SIZE` in `mariana/main.py:1180-1182` parsed
`AGENT_SETTLEMENT_RECONCILE_BATCH_SIZE` with a bare `int(os.getenv(...))` and
passed the result unvalidated into PostgreSQL `LIMIT $2` inside both
reconcilers (`mariana/agent/settlement_reconciler.py:107-120`,
`mariana/research_settlement_reconciler.py:60-74`).

PostgreSQL rejects negative LIMIT with
`asyncpg.exceptions.InvalidRowCountInLimitClauseError: LIMIT must not be
negative`. One bad operator env value (e.g. `-1`) bricked both settlement
daemons forever — every iteration raised before claiming rows, the outer
`except Exception` block in
`_run_settlement_reconciler_loop` / `_run_research_settlement_reconciler_loop`
logged the exception, slept the configured interval, and retried the same
broken value indefinitely. Stuck settlements never reconciled, every CC-04
/ T-01 / S-01-class repair surface was permanently disabled.

Reproduction (verified locally):

```python
await reconcile_pending_settlements(pool, max_age_seconds=300, batch_size=-1)
# raises asyncpg.exceptions.InvalidRowCountInLimitClauseError
```

---

## Fix — two layers

### Layer 1 — config parsing (`mariana/main.py:1180-1237`)

Replaced the bare `int(os.getenv(...))` with a new helper:

```python
def _parse_reconcile_batch_size(env_var: str, default: int) -> int:
    raw = os.getenv(env_var)
    if raw is None:
        logger.info("settlement_reconciler_batch_size_resolved", ..., source="default")
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        logger.warning("settlement_reconciler_batch_size_unparseable", ...)
        logger.info("settlement_reconciler_batch_size_resolved", ..., source="unparseable_fallback")
        return default
    clamped = max(1, parsed)
    logger.info(
        "settlement_reconciler_batch_size_resolved",
        ..., source="clamped" if clamped != parsed else "parsed",
    )
    return clamped


_SETTLEMENT_RECONCILE_BATCH_SIZE = _parse_reconcile_batch_size(
    "AGENT_SETTLEMENT_RECONCILE_BATCH_SIZE", 50
)
```

Behaviour table:

| `AGENT_SETTLEMENT_RECONCILE_BATCH_SIZE` | Resolved value | Source tag |
|----------------------------------------|----------------|------------|
| unset                                  | 50             | `default` |
| `"50"`                                 | 50             | `parsed` |
| `"0"`                                  | 1              | `clamped` |
| `"-1"`                                 | 1              | `clamped` |
| `"notanint"`                           | 50             | `unparseable_fallback` |
| `""`                                   | 50             | `unparseable_fallback` |
| `"999999"`                             | 999999         | `parsed` |

The helper emits exactly one `settlement_reconciler_batch_size_resolved`
info log per call so operators see the resolved value at daemon startup.

The research-settlement loop reuses the same module-level constant
(`_SETTLEMENT_RECONCILE_BATCH_SIZE`) so a single helper call covers both
daemons; no separate `RESEARCH_SETTLEMENT_RECONCILE_BATCH_SIZE` env var
exists in the codebase.

### Layer 2 — defensive function-entry guard

Added matching guards at the top of both reconcilers, before the SQL
fetch:

`mariana/agent/settlement_reconciler.py:81-95`:
```python
if batch_size <= 0:
    logger.debug(
        "settlement_reconciler_batch_size_clamped",
        requested=batch_size,
        clamped=1,
    )
    batch_size = 1
```

`mariana/research_settlement_reconciler.py:50-63`: identical guard with
the `research_settlement_reconciler_batch_size_clamped` event name.

These guards make the function safe to call directly from tests, future
internal callers, or any code path that bypasses the env helper.

---

## Diff summary

```
 mariana/agent/settlement_reconciler.py    | 16 +++++++++
 mariana/main.py                           | 59 +++++++++++++++++++++++++++++--
 mariana/research_settlement_reconciler.py | 15 ++++++++
 tests/test_cc05_reconciler_batch_size_validation.py | 487 +++++++++++++++++++++
 4 files changed, 574 insertions(+), 2 deletions(-)
```

---

## Tests added

File: `tests/test_cc05_reconciler_batch_size_validation.py` — **16 tests**.

| Test | Coverage |
|------|----------|
| `test_cc05_helper_unset_returns_default` | env var unset → default |
| `test_cc05_helper_unparseable_returns_default` | `"notanint"` → default |
| `test_cc05_helper_negative_clamps_to_one` | `"-1"` → 1 |
| `test_cc05_helper_zero_clamps_to_one` | `"0"` → 1 |
| `test_cc05_helper_positive_passthrough` | `"999999"` → 999999 |
| `test_cc05_helper_empty_string_falls_back` | `""` → default |
| `test_cc05_agent_reconciler_clamps_non_positive_batch_size[-1]` | agent reconciler -1 against real PG, must not raise InvalidRowCountInLimitClauseError, clamps to 1 |
| `test_cc05_agent_reconciler_clamps_non_positive_batch_size[0]` | agent reconciler 0, same |
| `test_cc05_agent_reconciler_clamps_non_positive_batch_size[-999]` | agent reconciler -999, same |
| `test_cc05_agent_reconciler_huge_batch_size_no_overflow` | agent reconciler 10**9, no overflow, processes normally |
| `test_cc05_research_reconciler_clamps_non_positive_batch_size[-1]` | research reconciler -1 against real PG |
| `test_cc05_research_reconciler_clamps_non_positive_batch_size[0]` | research reconciler 0 |
| `test_cc05_research_reconciler_clamps_non_positive_batch_size[-42]` | research reconciler -42 |
| `test_cc05_research_reconciler_huge_batch_size_no_overflow` | research reconciler 10**9 |
| `test_cc05_agent_clamp_is_function_entry_pure_unit` | pure-unit fake-pool, asserts the function never passes a non-positive LIMIT to PG |
| `test_cc05_research_clamp_is_function_entry_pure_unit` | pure-unit fake-pool research equivalent |

The DB-backed tests use a marker-fixup short-circuit (`ledger_applied_at`
seeded `now()`) so they exercise the real CTE+LIMIT query without
needing httpx mocking. The pure-unit tests pin the guard at the function
boundary independent of any DB behaviour, so a future refactor that
moves the clamp out of the env helper cannot regress this protection
silently.

---

## Pytest results

Baseline (HEAD `e8564f7`, before fix): `443 passed, 13 skipped` (456 collected).

After fix (HEAD `7ea5465`, including a parallel CC-04 fix that landed
between baseline measurement and this commit): `472 passed, 13 skipped`
(485 collected). Net +29 passes (+16 from this CC-05 fix; +13 from the
CC-04 fix that runs in parallel on the same branch). Zero failures.

```
$ PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb python -m pytest -q
...
472 passed, 13 skipped, 2 warnings in 7.78s
```

---

## Files changed

- `mariana/main.py` (env-helper added, single-source-of-truth)
- `mariana/agent/settlement_reconciler.py` (function-entry guard)
- `mariana/research_settlement_reconciler.py` (function-entry guard)
- `tests/test_cc05_reconciler_batch_size_validation.py` (new, 16 tests)
- `loop6_audit/REGISTRY.md` (CC-05 row added in this report)
- `loop6_audit/CC05_FIX_REPORT.md` (this report)

## Commit

```
commit 7ea5465a03b0281457cdaa5306fe7934b9d344bf
Author: fpkgvip <fpkgvip@gmail.com>
Date:   2026-04-28

    CC-05 fix reconciler negative/zero batch_size validation in config and at function entry
```
