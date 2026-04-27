# F-06 Fix Report — Intelligence Endpoint Pagination

**Date**: 2026-04-27  
**Branch**: `loop6/zero-bug`  
**Audit reference**: `loop6_audit/A6_phase_e_reaudit.md` — finding F-06  
**Status**: ✅ COMPLETE — all 5 endpoints paginated, all tests green

---

## Problem Statement

The five intelligence API endpoints returned unbounded result sets with no
pagination mechanism. On large investigations these endpoints could return
thousands of rows in a single response, causing:

1. **Memory pressure** on both the backend (asyncpg fetch of entire table) and
   the client (parsing a large JSON array).
2. **Timeout risk** — no upper bound on query execution time.
3. **No forward-navigation path** — callers could not request subsequent pages.

Confirmed by grep that no frontend code consumed these endpoints, so no
backward-compatibility constraints apply.

---

## Fix Summary

### Pagination Design

All five endpoints use **keyset (cursor-based) pagination** rather than
offset pagination, avoiding the `OFFSET N SKIP` performance cliff on large
tables.

**Cursor format**: `"{timestamp_iso}|{uuid}"` — a composite key that
uniquely identifies a position using the row's timestamp plus its UUID
primary key. The `|` delimiter is split on the first occurrence to handle
ISO timestamps that might contain `+` characters.

**Keyset query pattern** (for `(created_at, id)` ordering):
```sql
WHERE task_id = $1
  AND (created_at, id) > ($cursor_ts::timestamptz, $cursor_id::uuid)
ORDER BY created_at, id
LIMIT $limit + 1   -- fetch one extra to detect "has next page"
```
Fetching `limit + 1` rows lets the helper detect whether a next page exists
without an extra COUNT query. The extra row is not returned; its position is
encoded as the `next_cursor`.

**Response envelope** (all 5 endpoints):
```json
{
  "items":       [...],
  "next_cursor": "2026-04-27T12:34:56+00:00|some-uuid",
  "limit":       100
}
```
`next_cursor` is `null` on the last page.

---

### Shared constants and helpers — `api.py`

```python
_INTEL_LIMIT_QUERY  = Query(default=100, ge=1, le=1000)
_INTEL_CURSOR_QUERY = Query(default=None)

def _build_next_cursor(items, ts_key="created_at", id_key="id") -> str | None:
    """Encode last-item position as '{ts_iso}|{id}'."""
```

FastAPI `Query(ge=1, le=1000)` enforces a 422 Unprocessable Entity for
out-of-range values at the framework level. Helpers also clamp internally
(`max(1, min(limit, _INTEL_MAX_LIMIT))`) for defense-in-depth.

---

### Per-endpoint changes

#### `/api/intelligence/{task_id}/claims`

- **Helper**: `evidence_ledger.get_evidence_ledger(task_id, db, limit, cursor)`
- **Keyset**: `(created_at, id)` on table `claims`
- **Envelope extra fields**: none

#### `/api/intelligence/{task_id}/source-scores`

- **Helper**: `credibility.get_source_scores(task_id, db, limit, cursor)`
- **Keyset**: `(created_at, id)` on table `source_scores`
- **Envelope extra fields**: `average_credibility` (float, preserved from pre-pagination implementation)

#### `/api/intelligence/{task_id}/contradictions`

- **Helper**: `contradictions.get_contradiction_matrix(task_id, db, limit, cursor)`
- **Keyset**: `(created_at, id)` on table `contradiction_pairs`
- **Envelope extra fields**: `next_cursor` and `limit` injected into the existing matrix dict (which also contains `summary`, `contradictions`, `total_count` keys)

#### `/api/intelligence/{task_id}/hypotheses/rankings`

- **Helper**: `hypothesis_engine.get_hypothesis_rankings(task_id, db, limit, cursor)`
- **Keyset**: `(last_updated, id)` on table `hypothesis_priors` (the table has `last_updated` not `created_at`)
- **Cursor field names**: helper injects `_cursor_ts` and `_cursor_id` into each item dict; route extracts them to build `next_cursor` and strips them before returning. The extra fields are never surfaced to the client.
- **Envelope extra fields**: `winner` (hypothesis with highest probability)

#### `/api/intelligence/{task_id}/perspectives`

- **Pagination location**: inline in the route (no separate helper — `db.fetch()` is called directly in the route)
- **Keyset**: `(created_at, id)` on table `perspective_syntheses`
- **Bug fix included**: `.isoformat()` call guarded with `hasattr(..., 'isoformat')` to avoid `AttributeError` when date fields are already strings (e.g. when returned from JSON deserialization)

---

### Helper changes

All four modified helpers (`evidence_ledger.py`, `credibility.py`,
`contradictions.py`, `hypothesis_engine.py`) follow the same pattern:

```python
_INTEL_MAX_LIMIT     = 1000
_INTEL_DEFAULT_LIMIT = 100

async def get_evidence_ledger(
    task_id: str,
    db: Database,
    limit: int = _INTEL_DEFAULT_LIMIT,
    cursor: str | None = None,
) -> list[dict]:
    limit = max(1, min(limit, _INTEL_MAX_LIMIT))  # clamp
    if cursor:
        cursor_ts, cursor_id = cursor.split("|", 1)
        # keyset WHERE clause
    rows = await db.fetch(...)
    return [dict(r) for r in rows]
```

---

## Tests

**File**: `tests/test_f06_intel_pagination.py`

| # | Test | Result |
|---|------|--------|
| 1 | `test_default_limit_cap` | ✅ PASS |
| 2 | `test_explicit_limit` | ✅ PASS |
| 3 | `test_limit_clamping` | ✅ PASS |
| 4 | `test_cursor_pagination` | ✅ PASS |
| 5 | `test_intel_constants` | ✅ PASS |
| 6 | `test_claims_envelope_shape` | ✅ PASS |
| 7 | `test_source_scores_envelope_shape` | ✅ PASS |
| 8 | `test_contradictions_envelope_shape` | ✅ PASS |
| 9 | `test_hypothesis_rankings_envelope_shape` | ✅ PASS |
| 10 | `test_perspectives_envelope_shape` | ✅ PASS |

Test strategy: `app.dependency_overrides` patches `_get_current_user` and
`_require_investigation_owner` so no Supabase JWT is needed. Intelligence
helpers are patched via `unittest.mock.patch` to return deterministic fixture
data. `POSTGRES_DSN` env var is set before import to prevent config errors
when the test process has no live DB.

Full suite: **187 passed, 14 skipped** (no failures).

---

## No Breaking Changes

The audit confirmed (via grep) that no frontend code consumed any of the five
intelligence endpoints before this change. The response shape change from a
bare array `[...]` to `{"items": [...], "next_cursor": ..., "limit": N}` is
therefore non-breaking.

Clients that do not pass `limit` or `cursor` receive the default page size
(100) and `next_cursor: null` if there are ≤ 100 results — identical in
practice to the previous unbounded behaviour for small datasets.

---

## Files Changed

| File | Type | Change |
|------|------|--------|
| `mariana/api.py` | Modified | `_INTEL_LIMIT_QUERY`, `_INTEL_CURSOR_QUERY`, `_build_next_cursor()` helpers added; 5 route functions updated with `limit`/`cursor` params and envelope response |
| `mariana/orchestrator/intelligence/evidence_ledger.py` | Modified | `get_evidence_ledger()` signature: `limit`, `cursor`; keyset SQL; `_INTEL_MAX_LIMIT`, `_INTEL_DEFAULT_LIMIT` |
| `mariana/orchestrator/intelligence/credibility.py` | Modified | `get_source_scores()` signature: `limit`, `cursor`; keyset SQL; constants |
| `mariana/orchestrator/intelligence/contradictions.py` | Modified | `get_contradiction_matrix()` signature: `limit`, `cursor`; keyset SQL; constants |
| `mariana/orchestrator/intelligence/hypothesis_engine.py` | Modified | `get_hypothesis_rankings()` signature: `limit`, `cursor`; keyset on `(last_updated, id)`; `_cursor_ts`/`_cursor_id` injection; constants |
| `tests/test_f06_intel_pagination.py` | New | 10-test pagination regression suite |
