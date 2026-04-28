# Re-audit #37 — post-CC-09

**Range audited:** 7f635a9..6be5860 (CC-06 empty-dict fail-closed, CC-07 Product copy, CC-08 admin auth-header logging, CC-09 vault contract drift, Phase F batch 2 loading states)
**Auditor:** adversarial subagent
**Date:** 2026-04-28
**HEAD:** 6be5860

## Findings

### CC-10 [Severity P4]: CC-09 fixed `\Z` anchoring only for vault names, but sibling validators still use `$` and accept trailing-newline IDs / path components / env names
- File: mariana/tools/memory.py:29,85-86; sandbox_server/app.py:97-118,193-209
- Mechanism: CC-09 correctly changed the vault name regexes to `\Z`, but the adjacent identifier validators still use `$`-anchored patterns: `UserMemory` validates `user_id` with `_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")`, the sandbox validates `user_id` / workspace path components with `_SAFE_ID_RE` and `_PATH_COMPONENT_RE`, and `ExecRequest._check_env` validates env-var names with `re.match(r"^[A-Z_][A-Z0-9_]{0,63}$", k)`. In Python, `$` matches before a trailing newline, so these validators accept strings that violate their own documented grammar, reintroducing the exact trailing-newline contract hole CC-09 just closed for vault keys.
- Reproduction: in a repo-local Python check, all four sibling validators accepted newline-suffixed inputs: `_USER_ID_RE.match('abc\n')`, `_SAFE_ID_RE.match('abc\n')`, `_PATH_COMPONENT_RE.match('file.txt\n')`, and `re.match(r'^[A-Z_][A-Z0-9_]{0,63}$', 'FOO\n')` each returned truthy. That means newline-bearing `user_id`s can pass into the memory-path and sandbox-workspace gates, newline-bearing path components can pass `_safe_workspace_path`, and newline-bearing env names can pass the sandbox request validator.
- Recommended fix: apply the same CC-09 `\Z` hardening to these sibling validators (`_USER_ID_RE`, `_SAFE_ID_RE`, `_PATH_COMPONENT_RE`, and the sandbox env-name regex), then add regression tests that pin trailing-`\n` rejection on each surface.

### CC-11 [Severity P4]: `fetch_vault_env` still silently truncates oversize secret values instead of matching `validate_vault_env`’s fail-fast contract
- File: mariana/vault/runtime.py:95-110,275-300; tests/test_vault_runtime.py:57-59
- Mechanism: `validate_vault_env` rejects any value longer than `_MAX_VAULT_VALUE_LEN` (`16_384`) with `ValueError`, but `fetch_vault_env` does not enforce the same contract on the read path. After the CC-09 shape checks, the fetch loop still executes `out[k] = v[:_MAX_VAULT_VALUE_LEN]`, so an oversize stored secret is silently truncated and returned even under `requires_vault=True`. That is a fresh read/write contract drift on the same vault surface CC-09 was fixing: the write path says “oversize secret is invalid,” while the read path mutates it into a different secret and lets the task continue.
- Reproduction: in a repo-local Python check, a Redis payload containing `{"OPENAI_API_KEY": "x" * 20000}` produced a fetched value of length `16384`, while `validate_vault_env({"OPENAI_API_KEY": "x" * 20000})` raised `ValueError: vault_env: value for 'OPENAI_API_KEY' too long`. So the worker currently runs with a truncated secret where the ingest contract would have rejected the payload outright.
- Recommended fix: treat `len(v) > _MAX_VAULT_VALUE_LEN` the same way empty values are handled now — fail closed under `requires_vault=True`, and at minimum log+drop under `requires_vault=False`. Add a regression test that pins read-side rejection (or explicit drop) for oversize values so fetch/store semantics cannot drift again.

## Verification of CC-06 fix

The CC-06 empty-dict branch now looks correct.

- Task creation still derives `requires_vault` from the validated payload’s truthiness at mariana/agent/api_routes.py:508-511.
- The worker still forwards that bit into `fetch_vault_env(... requires_vault=requires_vault ...)` before any tool execution at mariana/agent/loop.py:1174-1185.
- `fetch_vault_env` now distinguishes the two modes cleanly for top-level empty dicts: `if not data:` raises `VaultUnavailableError(... empty_payload ...)` only when `requires_vault=True`, and returns `{}` when `requires_vault=False` at mariana/vault/runtime.py:254-266.
- The regression tests cover both required and optional modes for `b'{}'`, `b'{ }'`, and `b'  {}\n'` at tests/test_cc06_vault_empty_dict_fail_closed.py:71-122.

I did not find a new `requires_vault` short-circuit or off-by-one bug in the CC-06 branch itself.

## Verification of CC-08 logging

The CC-08 logger changes look clean.

- `_normalize_bearer_auth_header` now emits one `logger.warning(...)` per reject branch at mariana/api.py:246-275.
- Those calls log only fixed event names plus short `reason` tags (`missing`, `empty`, `wrong_scheme`, `empty_token`); they do not include the raw `Authorization` header, token bytes, or token substrings anywhere in the logged fields.
- The reason codes are internally consistent with the four branches, and I did not find a token-leak or raw-header side channel in this patch.

## Verification of CC-09 / Phase F batch 2 other angles

- The vault-name regex change itself landed consistently in both runtime and store validation at mariana/vault/runtime.py:116-119 and mariana/vault/store.py:136-145.
- Empty-string values are now handled consistently on the new CC-09 surface: write-side validation drops them at mariana/vault/runtime.py:105-107, and read-side fetch now fails closed under `requires_vault=True` or logs+skips under `requires_vault=False` at mariana/vault/runtime.py:268-299.
- I did not find an infinite vault/conversation skeleton: `useVault` marks `loaded = true` on fetch failure at frontend/src/hooks/useVault.ts:133-155, `Vault.tsx` renders `vault.loadError` as an alert at frontend/src/pages/Vault.tsx:100-107, and the Chat conversation loader clears its loading token in `finally` before the skeleton gate at frontend/src/pages/Chat.tsx:3255-3276.
- The new `VaultSkeleton` and Chat loading skeleton both include `role="status"`, `aria-live="polite"`, and `aria-busy="true"` at frontend/src/components/deft/vault/VaultSkeleton.tsx:14-18 and frontend/src/pages/Chat.tsx:3255-3261.
- `Product.tsx` no longer contains the prior forbidden `multi-hour build` copy; the line now reads `multi-hour run` in the landed tree.

## Verdict

2 findings (2 × P4). Streak resets: CC-10, CC-11.

| ID | Severity | Surface |
|---|---|---|
| CC-10 | P4 | Sibling validators still use `$`, so trailing-newline IDs / path components / env names pass validation outside the vault path |
| CC-11 | P4 | `fetch_vault_env` truncates oversize values instead of matching write-side rejection / fail-closed semantics |
