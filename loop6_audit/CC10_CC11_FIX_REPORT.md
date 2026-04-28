# CC-10 + CC-11 Fix Report

**Branch:** `loop6/zero-bug`
**HEAD before:** `6be5860`
**HEAD after CC-10:** `80a3cb4`
**HEAD after CC-11:** `83f1057`
**Date:** 2026-04-28
**Audit source:** `loop6_audit/A42_post_cc09_reaudit.md`

---

## CC-10 — Sibling validators using `$` accepted trailing-newline inputs

### Mechanism

CC-09 hardened the vault `_NAME_RE` from `^...$` to `^...\Z` because Python's
`$` matches before a trailing `\n`. The post-CC-09 re-audit (#37) found four
sibling identifier validators that still used `$` and therefore accepted
strings like `"abc\n"`, `"file.txt\n"`, `"FOO\n"` even though those bytes
violate each validator's documented grammar. A poisoned `user_id` could be
joined into a memory path or workspace path; a poisoned env-var name could
reach the sandbox child env.

### Validators fixed (audit-listed file:line)

| File | Symbol | Pattern (after) |
|---|---|---|
| `mariana/tools/memory.py:33` | `_USER_ID_RE` | `r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}\Z"` |
| `sandbox_server/app.py:101` | `_SAFE_ID_RE` | `r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}\Z"` |
| `sandbox_server/app.py:102` | `_PATH_COMPONENT_RE` | `r"^[A-Za-z0-9._][A-Za-z0-9._\- ]{0,254}\Z"` |
| `sandbox_server/app.py:211` | inline env-name regex in `ExecRequest._check_env` | `r"^[A-Z_][A-Z0-9_]{0,63}\Z"` |

### Additional sibling sites fixed beyond the audit's listed file:line

The audit told me to grep repo-wide for other anchored regexes and decide
per-regex. Two additional sites were the same shape (gating a user-supplied
identifier / hostname that gets joined into a path or used in a security
decision) and were hardened in the same CC-10 commit:

| File:line | Symbol | Why fixed |
|---|---|---|
| `mariana/api.py:1671` | `_SAFE_PREVIEW_TASK` | Gates the preview `task_id` that is joined into `_PREVIEW_ROOT_PATH / task_id` and used as the scoped cookie path. A trailing `\n` would have slipped past, the same shape as the listed sandbox path-component drift. |
| `mariana/connectors/sec_edgar_connector.py:236` | inline SSRF host regex on `parsed_host` | This is the SEC EDGAR SSRF guard. `urlparse().hostname` is normally well-formed but defense-in-depth on an SSRF gate should not rely on `$`. Same one-character fix. |

### Anchored regexes left as `$` (decision documented per the audit's "be judicious" clause)

| File:line | Symbol | Decision |
|---|---|---|
| `mariana/vault/store.py:63` | `_BYTEA_HEX_RE = re.compile(r"^\\x([0-9a-fA-F]*)$")` | **Left as `$`.** This is a transport-format parser for Postgres `bytea` hex strings returned by PostgREST (`\xDEADBEEF`). Values come from a trusted DB response, not user input. It is not an identifier / name / key validator. The audit explicitly said to be judicious; converting this would be churn without a security gain. |

### Tests added (CC-10)

`tests/test_cc10_validator_trailing_newline.py` — **12 tests, all passing**:

* `test_user_id_re_rejects_trailing_newline` — `_USER_ID_RE.match("abc\n")` is None
* `test_user_id_re_pattern_uses_z_anchor` — pattern source pin
* `test_user_memory_constructor_rejects_trailing_newline_user_id` — `UserMemory("abc\n", ...)` raises
* `test_safe_id_re_rejects_trailing_newline` — `_SAFE_ID_RE.match("abc\n")` is None
* `test_safe_id_re_pattern_uses_z_anchor` — pattern source pin
* `test_valid_user_id_helper_rejects_trailing_newline` — `_valid_user_id("abc\n")` is False
* `test_path_component_re_rejects_trailing_newline` — `_PATH_COMPONENT_RE.match("file.txt\n")` is None
* `test_path_component_re_pattern_uses_z_anchor` — pattern source pin
* `test_sandbox_env_name_inline_regex_rejects_trailing_newline` — inline regex pin
* `test_sandbox_exec_request_rejects_env_var_with_trailing_newline_in_name` — `ExecRequest(env={"FOO\n": ...})` raises `ValidationError`
* `test_safe_preview_task_re_rejects_trailing_newline` — pin for the additional `mariana/api.py` site
* `test_sec_edgar_host_re_rejects_trailing_newline` — pin for the additional `sec_edgar_connector.py` site

---

## CC-11 — `fetch_vault_env` silently truncated oversize values

### Mechanism

`mariana/vault/runtime.py:108-109` (`validate_vault_env`, the WRITE path)
rejects any value longer than `_MAX_VAULT_VALUE_LEN` (16384) with
`ValueError`. `fetch_vault_env` (the READ path) used to silently slice
`out[k] = v[:_MAX_VAULT_VALUE_LEN]` — so a corrupted/poisoned blob with
`{"OPENAI_API_KEY": "x" * 20000}` would round-trip as a 16384-byte truncated
secret and the worker would run with the wrong secret. That is contract
drift on the same vault surface CC-04 / CC-06 / CC-09 had been closing.

### Fix

`mariana/vault/runtime.py` now treats oversize values the same way empty
values were treated by CC-09: fail-closed under `requires_vault=True`,
warn-and-drop under `requires_vault=False`. The under-cap branch stores
`v` verbatim — no slicing. The diagnostic / warning carries the offending
key name and length but **never the value bytes**.

* `requires_vault=True` + `len(v) > _MAX_VAULT_VALUE_LEN`
  → raises `VaultUnavailableError("vault_env oversize_value for task <id>: key '<k>' (len=<n> > max=16384)")`
* `requires_vault=False` + `len(v) > _MAX_VAULT_VALUE_LEN`
  → returns dict without that key, plus a structured warning
    `extra={"task_id", "reason": "oversize_value", "key", "length", "max"}`
* `len(v) == _MAX_VAULT_VALUE_LEN` boundary → still allowed, no slicing
* `len(v) < _MAX_VAULT_VALUE_LEN` → `out[k] = v` verbatim (no slicing)

### Tests added (CC-11)

`tests/test_cc11_vault_oversize_value.py` — **7 tests, all passing**:

* `test_oversize_value_with_requires_vault_raises_with_reason` — `len = max+1` raises with reason `oversize_value`; key name in message; value bytes NOT in message
* `test_oversize_value_far_above_cap_with_requires_vault_raises` — audit-mirroring repro at `len=20_000`
* `test_oversize_value_without_requires_vault_drops_key_and_warns` — degrade-mode drops key and emits structured warning with `key` + `length` extras; full value not logged
* `test_value_exactly_at_max_len_is_allowed_with_requires_vault` — boundary at `len == max`, round-trips verbatim, no slicing
* `test_value_exactly_at_max_len_is_allowed_without_requires_vault` — boundary in degrade mode
* `test_mixed_payload_with_oversize_key_raises_no_partial_return` — mixed valid + oversize keys → fail-closed wholesale, no partial dict returned
* `test_under_cap_value_is_not_sliced` — pin: under-cap value with weird-but-valid characters round-trips verbatim (was previously masked by `v[:max]` no-op slicing)

---

## Verification

### Targeted re-run

```
PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb \
  python -m pytest tests/test_cc10* tests/test_cc11* tests/test_cc09* -v
```

→ **29 passed in 0.55s** (12 CC-10 + 7 CC-11 + 10 CC-09 regression-pin)

### Full suite

```
PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb python -m pytest -q
```

→ **509 passed, 13 skipped, 0 failed** (was 490 passed pre-fix; +19 =
12 CC-10 + 7 CC-11)

### Style

* `ruff format tests/test_cc10_validator_trailing_newline.py tests/test_cc11_vault_oversize_value.py`
  → 1 file reformatted (CC-11 test), 1 file left unchanged.
* Source files not run through `ruff format` because the existing tree is
  not ruff-formatted (running it churns 1000+ unrelated lines per file);
  edits match the surrounding style — 4-space indent, double quotes,
  triple-backtick comments — exactly mirroring CC-09's commit shape.

---

## Commits

| Hash | Subject |
|---|---|
| `80a3cb4` | CC-10 fix sibling validators using $ instead of \Z accepting trailing-newline inputs |
| `83f1057` | CC-11 fix fetch_vault_env silently truncating oversize values instead of fail-closed |

`HEAD` is now `83f1057` on `loop6/zero-bug`.

---

## Files touched

```
 mariana/api.py                                  |   5 +-
 mariana/connectors/sec_edgar_connector.py       |   5 +-
 mariana/tools/memory.py                         |   6 +-
 mariana/vault/runtime.py                        |  31 +-
 sandbox_server/app.py                           |  11 +-
 tests/test_cc10_validator_trailing_newline.py   | 200 (new)
 tests/test_cc11_vault_oversize_value.py         | 252 (new)
```
