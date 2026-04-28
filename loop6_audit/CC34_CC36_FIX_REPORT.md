# CC-34 through CC-36 Fix Report

**Date:** 2026-04-28
**Branch:** `loop6/zero-bug`
**Pre-fix HEAD:** `a5c6de1`
**Post-fix HEAD:** `b3b04ef`
**Audit input:** `loop6_audit/A50_post_cc33_reaudit.md`

---

## Scope

Three findings from the A50 re-audit (post-CC-33):

| ID | Severity | Class |
|----|----------|-------|
| CC-34 | P2 | sandbox-server/disk-quota/projection (CC-28 hold-fail re-fix) |
| CC-35 | P2 | agent-loop/error-code-contract-drift |
| CC-36 | P3 | sidecar/observability/text-logging |

All three are `FIXED`. Zero deferred items. The fourth A50 finding (info-severity backup/DR posture, Finding 4) is an operational-evidence gap rather than a code defect and is recorded in the registry trail; no code change is appropriate from this repo.

---

## Commits (sequential, fast-forward push)

| # | SHA | Message |
|---|------|---------|
| 1 | `ff98165` | CC-34 enforce projected workspace size on /fs/write and /exec |
| 2 | `f02216a` | CC-35 enforce canonical agent error code contract; add allow-list invariant test |
| 3 | `b3b04ef` | CC-36 standardize sandbox/browser sidecars on JSON structured logging |

Push was a clean fast-forward (`a5c6de1..b3b04ef  loop6/zero-bug -> loop6/zero-bug`). No `--force`. No parallel-agent collisions.

---

## Per-CC details

### CC-34 — workspace quota projection (CC-28 hold-fail re-fix)
**File:** `sandbox_server/app.py`
**Why:** A50 Finding 1 (Medium). The CC-28 implementation only refused workspaces that were already over cap. A workspace at `cap - 1 KiB` could still be pushed over the limit by a single `/fs/write` body or the source file written by `/exec`, defeating the production-safety goal of CC-28.

**Approach:** Extend `_enforce_workspace_quota(workspace_root, additional_bytes=0)` to test `current + additional_bytes > _MAX_WORKSPACE_BYTES` and raise HTTP 507 `workspace_full` BEFORE the write commits.

* `/fs/write` decodes the base64 payload first, computes `delta = len(decoded) - existing_file_size_if_any` so an overwrite that *shrinks* a file is correctly accounted as a non-positive delta, then calls `_enforce_workspace_quota(workspace_root, additional_bytes=max(delta, 0))`. After a successful write, `_workspace_size_cache_set()` refreshes the cached total so two rapid writes inside the cache TTL cannot both see the same pre-write size and both pass.
* `/exec` projects `len(req.code.encode("utf-8"))` for the source file the sandbox writes immediately. Runtime artifacts remain bounded by `MAX_STDOUT_BYTES` / `MAX_STDERR_BYTES` and the wall-clock timeout — that is the correct architectural seam (filesystem-level quotas would be the next layer; over-projecting runtime artifacts pre-exec would force the worst-case to coincide with the start-of-exec check, which would be a different and stricter contract than the audit asks for).

**Tests:** `tests/test_cc34_workspace_quota_projection.py` — 6 tests, all under `SANDBOX_SHARED_SECRET="cc34-test-secret"` and `SANDBOX_MAX_WORKSPACE_BYTES=4096` for tight deterministic coverage; `_AUTH_HEADERS` carries the secret because the sandbox auth middleware returns 503 without it.
* under-cap write succeeds
* projected-over-cap write returns 507 even when current size is under cap
* `/exec` with oversize source rejects
* overwrite that shrinks a file does not trip projection
* cache refresh prevents back-to-back writes from racing
* helper's `additional_bytes=0` path still rejects already-over-cap workspaces

### CC-35 — canonical agent error-code contract
**File:** `mariana/agent/loop.py`, plus a small invariant test that scans `mariana/agent/api_routes.py`
**Why:** A50 Finding 2 (Medium). CC-20 / CC-21 / CC-25 documented a stable canonical error_code contract, but several persistence sites still wrote free-form interpolated strings into `task.error` / `step.error`:
* `_budget_exceeded` returned `"budget_exhausted: spent ..."` / `"duration_exhausted: ..."`
* `_infer_failure` returned `"timed_out after ...ms"` / `"process killed (memory / signal)"` / `"non-zero exit code ..."` / `"HTTP ..."`
* deliver-failure path persisted `task.error = f"deliver_failed: {err}"`
* unrecoverable-failure path persisted `task.error = f"unrecoverable: step {id} — {err}"`

Downstream consumers therefore received a mix of canonical codes and free-form strings, defeating the stable wire contract.

**Approach:** Two module-level allow-lists in `mariana/agent/loop.py`:

```python
CANONICAL_TASK_ERROR_CODES = {
    "stop_requested", "budget_exhausted", "duration_exhausted",
    "planner_failed", "deliver_failed", "unrecoverable",
    "vault_unavailable", "vault_transport_violation", "loop_crash",
}
CANONICAL_STEP_ERROR_CODES = {
    "tool_error", "unexpected", "timed_out",
    "process_killed", "non_zero_exit", "http_error",
}
```

The module docstring is updated with the full canonical sets per CC-20 / CC-21 / CC-25 / CC-35.

* `_budget_exceeded` now returns `tuple[bool, str, dict[str, Any]]` so the structured detail (`spent`, `cap`, `duration_ms`, `duration_cap_ms`) flows into the structured logger via `extra=` instead of being interpolated into the persisted code.
* `_infer_failure` returns the canonical step codes (`timed_out`, `process_killed`, `non_zero_exit`, `http_error`) and emits a `logger.info(...)` with the original numeric / signal context attached.
* The 3 task-error halt sites (budget exhausted, deliver_failed, unrecoverable) persist the bare canonical code only. The human-readable per-step diagnostic remains visible via the SSE event stream and structured server logs, just not in the persisted `task.error` field that is part of the wire contract.

**Tests:** `tests/test_cc35_canonical_error_codes.py` — 13 tests
* 1 allow-list pin (the two sets are exactly what the docstring documents)
* 2 source-grep parametrized tests over `mariana/agent/loop.py` and `mariana/agent/api_routes.py` using an AST walker (`_collect_error_assignments()`) that finds every `task.error = "..."` and `step.error = "..."` literal and asserts membership in the canonical set; f-strings and BinOps are treated as violations so future contributors can't sneak free-form text back in
* 2 budget unit tests pinning the new tuple signature and the canonical codes
* 5 `_infer_failure` unit tests pinning the canonical codes for each failure shape

`tests/test_cc01_agent_loop_behavioural.py` was updated:
* `test_cc01_budget_exhausted_halts_task` now expects the new tuple signature.
* `test_cc01_step_unexpected_exception_marks_step_failed` patches `loop_mod.dispatch` directly instead of `dispatcher_mod.dispatch`. This is a pre-existing latent test-isolation bug that CC-34's `importlib.reload(sandbox_server.app)` happened to surface: `mariana/agent/loop.py:69` does `from mariana.agent.dispatcher import ToolError, dispatch`, which binds a separate module-level reference; patching the source module is not enough. Fixing it here keeps the regression suite stable across test orderings.

### CC-36 — sidecar JSON structured logging
**Files:** `sandbox_server/app.py`, `browser_server/app.py`
**Why:** A50 Finding 3 (Low). Both sidecars used `logging.basicConfig(...)` with a free-form text format while the orchestrator uses `structlog.processors.JSONRenderer()` (`mariana/main.py:84-85`). Production log aggregators could not parse sidecar records without bespoke regex, and structured `extra=` fields attached at sidecar call sites (workspace path, user_id, request shape, reason tokens established by CC-18 / CC-19) were silently dropped by the text formatter.

**Approach:** Both sidecars now expose `_JsonLogFormatter` (a `logging.Formatter` subclass that emits one JSON object per record with `ts` / `level` / `logger` / `msg` plus any structured `extra=` keys; exception traces serialised under `exc_info`; non-JSON-serialisable extras coerced via `repr` so the formatter never crashes on weird kwargs) and `_configure_logging()` which wires the root logger based on `LOG_FORMAT=json|text`, default `json`. `LOG_FORMAT=text` falls back to the legacy human-readable format for local debugging.

Implementation is deliberately self-contained inside each sidecar — neither imports `structlog` from `mariana/`. Sidecars run in their own Docker containers (`mariana-sandbox`, `mariana-browser`) and their dependency footprint is intentionally minimal; coupling them to the orchestrator's structlog stack would invert the container boundary. The formatters are intentionally identical in spirit (slight wording differences in fallback text format reflect each sidecar's pre-existing legacy format), and the duplication is at most a few dozen lines.

`_configure_logging()` removes any pre-existing root handlers before installing the new one, so test reloads (e.g. CC-34's `importlib.reload`) do not leave double-emitting handlers in place.

**Tests:** `tests/test_cc36_sidecar_json_logging.py` — 7 tests, parametrized over `["sandbox_server.app", "browser_server.app"]`. The browser sidecar imports playwright at top-level and playwright is a sidecar-container dependency not installed in the orchestrator's CI, so the parametrisation conditionally skips the browser case via `importlib.util.find_spec("playwright")`. The sandbox sidecar has no such dependency and is always exercised. `WORKSPACE_ROOT` is set to a tempdir at module scope so `sandbox_server.app`'s import-time `mkdir` does not fail under the `/workspace` default.

* each sidecar exports `_JsonLogFormatter` and `_configure_logging`
* INFO records emit parseable JSON with required fields (`ts`, `level`, `logger`, `msg`)
* `extra=` fields round-trip as top-level JSON keys
* exception path serialises traceback under `exc_info` (with class name + message intact)
* non-JSON-serialisable extras coerced via `repr` without crashing the formatter
* `LOG_FORMAT=text` falls back to legacy format
* default (no env) installs the JSON formatter

---

## Verification

| Check | Result |
|-------|--------|
| `pytest -q` (full suite, Postgres at `/tmp:55432`) | **573 passed / 11 skipped / 0 failed** (was 547/11/0 pre-fix; +6 CC-34, +13 CC-35, +7 CC-36) |
| `tests/test_cc34_*` | 6/6 |
| `tests/test_cc35_*` | 13/13 |
| `tests/test_cc36_*` | 7/7 |
| `tests/test_cc01_*` (regression suite cross-check) | 8/8 |
| `tests/test_cc28_*` (CC-28 still holds under CC-34's stricter helper) | green |
| `tests/test_cc20_*`, `tests/test_cc21_*`, `tests/test_cc25_*` (canonical-code siblings) | green |
| `ruff format` on every changed Python file | clean |
| `ruff check` on every changed Python file | only pre-existing F401s (`asyncio`, `AgentTask`, `urlunparse`) — confirmed unchanged from pre-fix HEAD |

No frontend changes; vitest / lint / tsc not re-run for these CCs (last run at `a5c6de1`: 144/144 vitest, 0 lint errors).

---

## Decisions / non-goals

* **`/exec` projection:** projecting only the source-file size (not worst-case runtime artifacts) is intentional. Capping by worst-case stdout/stderr would force every `/exec` to allocate `MAX_STDOUT_BYTES + MAX_STDERR_BYTES` against the quota up-front, which would push small scripts over the limit on workspaces with anything else cached. The right next layer for runtime-artifact bounding is filesystem-level quota (XFS project quotas or tmpfs `size=`) inside the container, not orchestration logic in the route. Tracked as a follow-on if the audit returns to it.
* **Sidecar logging duplication:** the `_JsonLogFormatter` is duplicated between `sandbox_server/app.py` and `browser_server/app.py` rather than extracted into a shared package. Sidecars run as separate Docker containers and the duplication preserves their independence; a `mariana_sidecar_common` package would couple their build+deploy and is not warranted for ~70 lines of logging glue.
* **Backup / DR (A50 Finding 4):** info-severity, evidence-of-absence rather than evidence-of-defect, and out of scope for code changes. Recorded in the audit trail.

---

## Deferred items

**NONE.**
