# CC-04 Fix Report — vault malformed payload fail-closed bypass

**Severity:** P2
**Branch:** `loop6/zero-bug`
**Commit:** `6e8eb90`
**Date:** 2026-04-28
**Source audit:** `loop6_audit/A40_phase_d_post_cc02_reaudit.md` (re-audit #35, gpt_5_4)

---

## 1. Bug

`mariana/vault/runtime.py:220-225` — `fetch_vault_env(..., requires_vault=True)` raised `VaultUnavailableError` on Redis miss / transport failure but silently returned `{}` on three other failure modes:

1. **Malformed JSON** — `json.loads` raises (e.g. raw payload `b'{'`).
2. **Non-object JSON** — top-level value is not a dict (e.g. `b'[]'`, `b'"just-a-string"'`, `b'42'`).
3. **Invalid kv shape** — JSON object whose keys/values are not strings, or whose keys do not match the `_NAME_RE` env-var grammar.

`run_agent_task()` (`mariana/agent/loop.py:1179-1222`) aborts only on `VaultUnavailableError`, so a task with `requires_vault=True` silently ran with empty env when the Redis blob was corrupted, truncated, or poisoned — re-opening the exact U-03 fail-closed surface.

**Reproduction (pre-fix):** with a fake Redis client returning `b'{'`, `b'[]'`, or `b'"just-a-string"'`, `fetch_vault_env(BadRedis(...), 'task-1', requires_vault=True, redis_url='redis://localhost:6379/0')` returned `{}` instead of raising.

---

## 2. Fix location

- File: `mariana/vault/runtime.py`
- Function: `fetch_vault_env`
- Lines (post-fix): **221–268** (the JSON decode + payload-shape block at the end of the function)

A `logging.getLogger(__name__)` was also added at module top so the `requires_vault=False` corruption branches can emit a structured warning.

---

## 3. Diff summary

```
mariana/vault/runtime.py    | 45 +++++++++++++++++++++++++++++++++++++++++----
tests/test_cc04_vault_malformed_payload_fail_closed.py | new (211 lines)
```

### `mariana/vault/runtime.py`

- Added `import logging` and `logger = logging.getLogger(__name__)` at module scope.
- Replaced the silent `except: return {}` / `if not isinstance(data, dict): return {}` block with three explicit, distinct branches:

  | Failure mode      | `requires_vault=True`                                    | `requires_vault=False`                              |
  |-------------------|----------------------------------------------------------|-----------------------------------------------------|
  | `json.loads` raises | `raise VaultUnavailableError("... malformed_payload ...")` | `logger.warning(reason="malformed_payload")` + `{}` |
  | non-dict top-level  | `raise VaultUnavailableError("... non_object_payload ...")` | `logger.warning(reason="non_object_payload")` + `{}` |
  | bad kv shape inside dict | `raise VaultUnavailableError("... invalid_kv_shape ...")` | `logger.warning(reason="invalid_kv_shape")` + drop entry |

- Each error message embeds a distinct reason code (`malformed_payload` / `non_object_payload` / `invalid_kv_shape`) so ops can alert separately from transport failures.
- The cap-enforcement loop now uses the same kv-shape check; valid entries pass through unchanged so the `requires_vault=False` legacy path still tolerates a partially-corrupt blob and keeps the good entries (with a warning).

### `tests/test_cc04_vault_malformed_payload_fail_closed.py` (new)

Async tests using `pytest-asyncio` `auto` mode (matches `pytest.ini`); fixtures mirror `tests/test_u03_vault_redis_safety.py` (a `_StubRedis` whose `get()` returns a caller-supplied raw payload).

---

## 4. Tests added

13 tests, all passing:

**`requires_vault=True` fail-closed (7):**
1. `test_malformed_json_with_requires_vault_raises` — `b'{'` → `VaultUnavailableError("... malformed_payload ...")`
2. `test_list_payload_with_requires_vault_raises` — `b'[]'` → `... non_object_payload ...`
3. `test_string_payload_with_requires_vault_raises` — `b'"just-a-string"'` → `... non_object_payload ...`
4. `test_number_payload_with_requires_vault_raises` — `b'42'` → `... non_object_payload ...`
5. `test_non_string_value_inside_dict_with_requires_vault_raises` — `{"OPENAI_API_KEY": 12345}` → `... invalid_kv_shape ...`
6. `test_invalid_key_grammar_with_requires_vault_raises` — `{"lowercase_bad": "..."}` → `... invalid_kv_shape ...`
7. `test_value_list_inside_dict_with_requires_vault_raises` — `{"OPENAI_API_KEY": ["list"]}` → `... invalid_kv_shape ...`

**`requires_vault=False` legacy degrade + warning (4):**
8. `test_malformed_json_without_requires_vault_returns_empty_and_warns` — `b'{'` → `{}` + warning(reason=`malformed_payload`)
9. `test_list_payload_without_requires_vault_returns_empty_and_warns` — `b'[]'` → `{}` + warning(reason=`non_object_payload`)
10. `test_string_payload_without_requires_vault_returns_empty_and_warns` — `b'"..."'` → `{}` + warning(reason=`non_object_payload`)
11. `test_invalid_kv_shape_without_requires_vault_drops_entry_and_warns` — bad+good entry → bad dropped, good kept, warning(reason=`invalid_kv_shape`)

**Sanity (2):**
12. `test_well_formed_payload_round_trips_under_requires_vault`
13. `test_well_formed_payload_round_trips_without_requires_vault`

---

## 5. pytest counts

| Stage   | Command                                                                          | Result                              |
|---------|----------------------------------------------------------------------------------|-------------------------------------|
| Before  | `PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb python -m pytest -q` | `443 passed, 13 skipped` (HEAD `e8564f7`) |
| After   | same                                                                             | `472 passed, 13 skipped` (HEAD `6e8eb90`) |

The intermediate CC-05 fix (commit `7ea5465`) added 16 reconciler tests between the baseline and this fix, and CC-04 itself adds 13.  443 (baseline) + 16 (CC-05) + 13 (CC-04) = 472.  Zero failures.

Existing U-03 (`tests/test_u03_vault_redis_safety.py`, 7 tests) and CC-03 (`tests/test_cc03_vault_encryption_rls.py`, 22 tests) vault tests all still pass.

---

## 6. Backwards compatibility

- Tasks created without a vault (`requires_vault=False`) keep the legacy degrade-to-`{}` behaviour even when the Redis blob is corrupted, so a poisoned blob from a previous run cannot kill an unrelated task. A warning is emitted with the corruption reason so ops can clean it up.
- Tasks with `requires_vault=True` now uniformly fail-closed across **all** failure modes (transport, miss, malformed JSON, non-object, bad kv shape). The error message pattern matches the existing U-03 contract that `mariana/agent/loop.py:1185-1206` already converts to a clean task failure with a refunded reservation (V-02 path).

---

## 7. References

- Audit finding: `loop6_audit/A40_phase_d_post_cc02_reaudit.md` lines 9–13
- Companion U-03 fix: `tests/test_u03_vault_redis_safety.py` (transport / miss path)
- Companion CC-03 fix: `tests/test_cc03_vault_encryption_rls.py` (vault store encryption + RLS)
- Registry row added: `loop6_audit/REGISTRY.md` (CC-04 row, between T-01 and CC-05)
