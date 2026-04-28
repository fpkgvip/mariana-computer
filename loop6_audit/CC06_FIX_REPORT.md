# CC-06 Fix Report — empty-dict vault payload bypass under `requires_vault=True`

**Severity:** P2
**Branch:** `loop6/zero-bug`
**Commit:** `65da7b5`
**Date:** 2026-04-28
**Source audit:** `loop6_audit/A41_post_cc05_reaudit.md` (re-audit #36)

---

## 1. Bug

`mariana/vault/runtime.py` around lines 211 / 240 / 253 — CC-04 added three fail-closed corruption branches in `fetch_vault_env` (`malformed_payload`, `non_object_payload`, `invalid_kv_shape`), but it left a fourth escape: an empty-object payload literally `b'{}'` (or any whitespace equivalent like `b'{ }'` / `b'  {}\n'`) bypassed every check:

  1. `not raw` (line 211) was False — the bytes are non-empty.
  2. `json.loads('{}')` returned `{}` — no decode error.
  3. `isinstance({}, dict)` was True — passes the shape branch.
  4. The kv-shape for-loop at line 253 ran **zero iterations** — no entries to fail on.

So `out` remained the freshly-allocated `{}` and was returned silently. With `requires_vault=True`, the caller in `mariana/agent/loop.py:1179-1185` received `{}` (no `VaultUnavailableError`), `if vault_env:` at loop.py:1221 was False (no log), and the agent loop proceeded into planning/execution with an **empty env** — re-opening the exact U-03 fail-closed surface that U-03 + CC-04 were meant to close.

`store_vault_env` short-circuits `if not env: return` at runtime.py:145, so the only paths to a stored `{}` blob are corruption or external poisoning. Treating it as fail-closed under `requires_vault=True` is correct.

**Reproduction (pre-fix):**

```python
class _R:
    async def get(self, *_): return b'{}'
await fetch_vault_env(_R(), 'task-1', requires_vault=True, redis_url='redis://localhost:6379/0')
# returned {} silently; should raise VaultUnavailableError
```

---

## 2. Fix location

- File: `mariana/vault/runtime.py`
- Function: `fetch_vault_env`
- Lines (post-fix): **251–263** (new fourth branch, between the dict-shape guard at 240 and the kv-shape loop at 265)

---

## 3. Diff summary

```
mariana/vault/runtime.py                              | 17 ++++++++++++++++-
tests/test_cc06_vault_empty_dict_fail_closed.py       | new (172 lines)
```

### `mariana/vault/runtime.py`

Added a fourth fail-closed branch after the `not isinstance(data, dict)` check and before the kv-shape loop:

```python
# CC-06 fix: an empty-object payload (``{}``) under ``requires_vault=True``
# is the same fail-closed bypass shape U-03 + CC-04 were meant to close —
# the for-loop below would run zero iterations and silently return ``{}``.
# ``store_vault_env`` short-circuits ``if not env: return``, so a stored
# ``{}`` blob can only come from corruption / external poisoning; treat it
# as fail-closed too.  Under ``requires_vault=False`` an empty dict is the
# legitimate "no vaulted secrets" state and is preserved.
if not data:
    if requires_vault:
        raise VaultUnavailableError(
            f"vault_env empty_payload for task {task_id} that required vault"
        )
    return {}
```

The error message embeds a distinct reason code `empty_payload` so ops can alert separately from `malformed_payload` / `non_object_payload` / `invalid_kv_shape` / transport failures (matches the CC-04 reason-code pattern).

Under `requires_vault=False`, an empty dict is treated as the legitimate "no vaulted secrets" state — returned without a warning (different from the CC-04 corruption branches, which DO warn, because an empty dict is not a corruption signal when the task never registered any vault_env).

### `tests/test_cc06_vault_empty_dict_fail_closed.py` (new)

Async tests using `pytest-asyncio` `auto` mode (matches `pytest.ini`); fixtures mirror `tests/test_cc04_vault_malformed_payload_fail_closed.py` (a `_StubRedis` whose `get()` returns a caller-supplied raw payload).

---

## 4. Tests added

8 tests, all passing:

**`requires_vault=True` fail-closed (3):**
1. `test_empty_dict_payload_with_requires_vault_raises` — `b'{}'` → `VaultUnavailableError("... empty_payload ...")`
2. `test_whitespace_inside_empty_dict_with_requires_vault_raises` — `b'{ }'` → `... empty_payload ...`
3. `test_padded_empty_dict_with_requires_vault_raises` — `b'  {}\n'` → `... empty_payload ...`

**`requires_vault=False` legitimate "no secrets" (2):**
4. `test_empty_dict_payload_without_requires_vault_returns_empty` — `b'{}'` → `{}`
5. `test_whitespace_empty_dict_without_requires_vault_returns_empty` — `b'{ }'` → `{}`

**Cross-contract (1):**
6. `test_nested_empty_dict_falls_under_cc04_invalid_kv_shape` — `b'{"x": {}}'` is NOT empty (has key `"x"` with non-string value `{}`) → falls under existing CC-04 `invalid_kv_shape` branch, NOT `empty_payload`. Documents the boundary between CC-04 and CC-06.

**Sanity (2):**
7. `test_well_formed_payload_still_round_trips_under_requires_vault`
8. `test_well_formed_payload_still_round_trips_without_requires_vault`

---

## 5. pytest counts

| Stage  | Command                                                                          | Result                              |
|--------|----------------------------------------------------------------------------------|-------------------------------------|
| Before | `PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb python -m pytest -q` | `472 passed, 13 skipped` (HEAD `52c436d`) |
| After  | same                                                                             | `480 passed, 13 skipped` (HEAD `65da7b5`) |

CC-06 itself adds 8 tests. 472 (baseline) + 8 (CC-06) = 480. Zero failures.

Targeted run (CC-04 + CC-06 + U-03):

```
tests/test_cc04_vault_malformed_payload_fail_closed.py .............     [ 46%]
tests/test_cc06_vault_empty_dict_fail_closed.py ........                 [ 75%]
tests/test_u03_vault_redis_safety.py .......                             [100%]
============================== 28 passed in 0.46s ==============================
```

Existing U-03 (7 tests), CC-03 (`tests/test_cc03_vault_encryption_rls.py`, 22 tests), and CC-04 (13 tests) vault tests all still pass.

---

## 6. Backwards compatibility

- Tasks created without a vault (`requires_vault=False`) keep returning `{}` for an empty-object payload — this is the legitimate "no vaulted secrets" state, not a corruption signal. No behaviour change for these tasks.
- Tasks with `requires_vault=True` now uniformly fail-closed across **all five** failure modes (transport, miss, malformed JSON, non-object top-level, bad kv shape, **empty object**). The error message pattern matches the existing U-03 / CC-04 contract that `mariana/agent/loop.py:1185-1206` already converts to a clean task failure with a refunded reservation (V-02 path).
- Nested empty (`b'{"x": {}}'`) is unaffected: it has a top-level entry, so the new `if not data:` guard does not fire; instead the existing CC-04 `invalid_kv_shape` guard rejects the non-string value `{}` (test #6 above pins this).

---

## 7. References

- Audit finding: `loop6_audit/A41_post_cc05_reaudit.md` lines 10–31 (CC-06)
- Companion U-03 fix: `tests/test_u03_vault_redis_safety.py` (transport / miss path)
- Companion CC-03 fix: `tests/test_cc03_vault_encryption_rls.py` (vault store encryption + RLS)
- Companion CC-04 fix: `tests/test_cc04_vault_malformed_payload_fail_closed.py` (malformed / non-object / bad-kv corruption shapes)
- Registry row added: `loop6_audit/REGISTRY.md` (CC-06 row, after CC-05)
