# CC-37 / CC-38 Fix Report

**Date:** 2026-04-28
**Branch:** `loop6/zero-bug`
**Pre-fix HEAD:** `9ee57f2`
**Post-fix HEAD:** (final docs commit pushed at end of batch)
**Audit input:** `loop6_audit/A51_post_cc36_reaudit.md`

---

## Scope

Two low-severity findings from the A51 re-audit (post-CC-36):

| ID | Severity | Class |
|----|----------|-------|
| CC-37 | P4 (Low) | sandbox-server / cache / unbounded-growth |
| CC-38 | P4 (Low) | sidecar / observability / log-field-parity |

Both are `FIXED`. Zero deferred items in code scope. The third A51
finding (info-severity backup/DR posture, Finding 3) is operational /
runbook scope rather than a code defect; it remains carried over from
A50 Finding 4 and is recorded in the audit trail without a code change.

---

## Commits (sequential, fast-forward push)

| # | SHA | Message |
|---|------|---------|
| 1 | `216469b` | CC-37 bound _WORKSPACE_SIZE_CACHE with TTL+FIFO eviction |
| 2 | `e6a0cc7` | CC-38 align sidecar JSON log field names with structlog (event/timestamp) |
| 3 | (final docs commit) | loop6 docs: CC-37/CC-38 registry rows + fix report + A51 audit input |

Push was a clean fast-forward (`9ee57f2..` → `loop6/zero-bug`). No
`--force`. No parallel-agent collisions.

---

## Per-CC details

### CC-37 — bound `_WORKSPACE_SIZE_CACHE` with TTL + FIFO eviction
**File:** `sandbox_server/app.py`
**Why:** A51 Finding 1 (Low). `_WORKSPACE_SIZE_CACHE` was a plain `dict`
keyed by workspace path string (which embeds `user_id`). Entries were
refreshed in place by `_workspace_size_cache_set()` and on cache miss in
`_workspace_size_bytes()`, but never evicted by size. A long-running
sandbox container serving N distinct user_ids would grow the dict
linearly in N — same unbounded-cache class CC-30 closed for
`_ADMIN_ROLE_CACHE` in `mariana/api.py`. Realistic ceiling around a
few hundred MB at 1M distinct users — not an immediate availability
risk, but a quiet memory leak.

**Approach:** inline a `_BoundedTTLCache` class in `sandbox_server/app.py`
parametrised for `(monotonic_inserted_at, size_bytes)` value tuples.
Mirrors the CC-30 cache class in `mariana/api.py:156-209` so the two
caches behave alike. FIFO eviction via `OrderedDict` (oldest insertion
drops on overflow), TTL of `_WORKSPACE_SIZE_TTL_SEC = 5.0` enforced
inside `get()` so a stale post-write size never feeds the quota helper.

* `maxsize = _WORKSPACE_SIZE_CACHE_MAX_ENTRIES = 10_000` — generous
  for realistic tenancy and well under 10 MB worst-case footprint.
* The class exposes the dict subset the existing call sites need:
  `get(key)` (TTL-evicting), `__setitem__(key, value)` (FIFO-evicting
  on overflow, refreshes order on re-set), `__getitem__(key)`
  (unconditional read used by test inspection), `pop(key, default)`
  (preserved for CC-28 test compatibility), `clear()`, `__len__`,
  `__contains__`. The shape is the minimum subset existing tests and
  production code need.
* Inlined rather than shared with `mariana/api.py` because the sandbox
  runs in its own Docker container and is intentionally decoupled from
  orchestrator imports; the duplication is ≈40 lines and matches the
  same pattern as CC-36's `_JsonLogFormatter` duplication.
* All three call sites updated in place: `_workspace_size_bytes` get +
  set, `_workspace_size_cache_set` post-write refresh. The call shapes
  are identical to the prior dict (`cache.get(key)`, `cache[key] = ...`,
  `cache.clear()`) so the swap is drop-in.

**Tests:** `tests/test_cc37_workspace_size_cache_bound.py` — 5 tests:

* `test_cc37_bounded_eviction_fifo` — inserting `maxsize + 2` distinct
  keys keeps `len(cache) == maxsize`, oldest two are evicted in order;
* `test_cc37_module_cache_is_bounded` — module-level instance is a
  `_BoundedTTLCache`, capacity and TTL constants pinned;
* `test_cc37_ttl_expiry_evicts_on_get` — entry older than TTL is
  reported as miss AND dropped from the cache (uses monkeypatched
  `time.monotonic` for determinism);
* `test_cc37_workspace_size_bytes_hit_miss` — public helper returns
  the right size on miss, returns the same (stale-by-design) value
  during the TTL window even if the FS changed, and returns the fresh
  total after `clear()`;
* `test_cc37_cc34_quota_projection_still_holds` — regression guard
  that the projected-quota path still raises HTTP 507 `workspace_full`
  after the cache swap.

### CC-38 — align sidecar JSON log field names with structlog
**Files:** `sandbox_server/app.py`, `browser_server/app.py`,
`tests/test_cc36_sidecar_json_logging.py` (assertions tightened),
`tests/test_cc38_sidecar_log_field_parity.py` (new).
**Why:** A51 Finding 2 (Low). Both sidecars' `_JsonLogFormatter`s
emitted `{"ts", "level", "logger", "msg", ...}` while the orchestrator
configures structlog at `mariana/main.py:79-85` with
`TimeStamper(fmt="iso")` + `JSONRenderer()`, which emits `{"event",
"timestamp", "level", "logger", ...}`. Cross-service log aggregation
(ELK / Datadog / Loki) had to apply per-emitter format-translation
rules to query both record types under the same schema. Acknowledged
in `loop6_audit/CC34_CC36_FIX_REPORT.md:102-106` as an intentional
independence trade-off, but A51 elevated it to a real operator-visible
mismatch worth aligning.

**Approach:** rename the two payload literals in each sidecar formatter:

* `"msg"` → `"event"` (matches structlog `JSONRenderer()`'s default
  message key);
* `"ts"` → `"timestamp"` (matches structlog `TimeStamper(fmt="iso")`'s
  output key).

The change is minimal and self-contained — sidecars still don't import
structlog. Both sidecars now emit the identical canonical schema as
the orchestrator. Repo-wide grep confirmed no consumer parses `msg` /
`ts` keys from sidecar logs, so the rename is a one-shot. The
`_JSON_RESERVED` set in each sidecar still contains `msg` (a LogRecord
attribute name, used during the `record.__dict__` iteration to skip
the LogRecord's own message field) — that's unrelated to the payload
output keys and was left as-is.

**Tests:**

* `tests/test_cc36_sidecar_json_logging.py:test_cc36_info_record_emits_valid_json`
  was tightened: now asserts `event` / `timestamp` keys are present
  and explicitly forbids legacy `msg` / `ts` keys, so a revert
  immediately fails the existing CC-36 test surface.
* `tests/test_cc38_sidecar_log_field_parity.py` — 4 new tests,
  parametrised over `["sandbox_server.app", "browser_server.app"]`
  with `find_spec("playwright")` skip on the browser case (matching
  CC-36's parametrisation):
  * `test_cc38_sidecar_emits_event_and_timestamp` — every formatted
    record carries `event` + `timestamp` + `level` as top-level JSON
    keys;
  * `test_cc38_sidecar_does_not_emit_legacy_fields` — neither legacy
    `msg` nor `ts` appears, with or without an `extra=` payload, and
    `extra=` round-trip is preserved (CC-36 invariant);
  * `test_cc38_formatter_source_uses_canonical_keys` — paranoid
    revert guard: `inspect.getsource(_JsonLogFormatter.format)` must
    contain `"event"` and `"timestamp"` literals AND must not contain
    `"msg":` or `"ts":` literals (catches a payload-dict revert that a
    runtime-only test could miss);
  * `test_cc38_orchestrator_structlog_uses_canonical_keys` — sanity
    pin on the other side of the parity claim: `mariana/main.py`
    source must still configure `TimeStamper(fmt="iso")` and
    `JSONRenderer()`, so the alignment doesn't silently rot if the
    orchestrator side changes.

---

## Verification

| Check | Result |
|-------|--------|
| `pytest -q` (full suite) | **582 passed / 11 skipped / 0 failed** (was 573/11/0 pre-fix; +5 CC-37, +4 CC-38) |
| `tests/test_cc37_*` | 5/5 |
| `tests/test_cc38_*` | 4/4 |
| `tests/test_cc36_*` (tightened assertions still pass) | 7/7 |
| `tests/test_cc28_*` (CC-28 quota regression) | green |
| `tests/test_cc34_*` (CC-34 projected-quota regression) | green |
| `npm run lint` (frontend) | 0 errors / 27 pre-existing warnings (unchanged from pre-fix HEAD) |
| `npx tsc --noEmit` (frontend) | clean |
| `npx vitest run` (frontend) | 144/144 (15 test files) |
| `ruff format` / `ruff check` on changed Python files | clean (only pre-existing `urllib.parse.urlunparse` F401 in `browser_server/app.py` — confirmed unchanged from pre-fix HEAD) |

---

## Decisions / non-goals

* **`_BoundedTTLCache` inlined, not shared.** `mariana/api.py` already
  has a near-identical `_BoundedTTLCache` for `_ADMIN_ROLE_CACHE`
  (CC-30). Sharing would couple the sidecar container to the
  orchestrator import surface and re-introduce exactly the
  separation A50 asked us to preserve in CC-36's `_JsonLogFormatter`
  decision. The duplication is ~40 lines and matches the existing
  precedent.
* **`__getitem__` and `pop` retained on the new class.** The CC-28 and
  CC-34 test suites reference both via the module-level cache. Keeping
  the API shape is what makes the swap drop-in. The production code
  paths still use only `get()` so the TTL contract is honoured where
  it matters.
* **No structlog import in sidecars.** CC-38 aligns the *output schema*
  without coupling the sidecar containers to the orchestrator's
  structlog stack. The sidecars stay self-contained behind their
  own Docker images.
* **Backup / DR (A51 Finding 3, carried over from A50 Finding 4):**
  info-severity, evidence-of-absence rather than evidence-of-defect.
  Operational scope, not code. No code change in this batch.

---

## Deferred items

**NONE in code scope.** The A51 info finding (backup / DR posture)
remains carried-over operational documentation work, recorded in the
registry trail.
