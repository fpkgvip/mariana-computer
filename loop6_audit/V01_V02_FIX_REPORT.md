# V-01 / V-02 Fix Report — vault Redis hostname validation and worker fail-closed settlement

- Severity: P2 + P2
- Surface: vault Redis transport policy, cache Redis transport policy, agent worker vault bootstrap and settlement
- Branch: `loop6/zero-bug`
- Fixed: 2026-04-28

---

## Summary

This patch closes two related bugs on the vault / agent-loop boundary.

- V-01: the Redis URL validator used substring matching on the raw URL, so hostile userinfo or subdomains could masquerade as local Redis and bypass the TLS requirement.
- V-02: the worker-side fail-closed path for `requires_vault=True` returned before the outer `finally:` block ran, so `_settle_agent_credits` never executed and reserved credits stayed stranded.

Both fixes land together because they share the same runtime surface and test harness.

---

## Root cause — V-01

The U-03 vault transport fix and the earlier cache transport fix both validated Redis URLs by lowercasing the raw URL string and checking whether any token such as `://localhost`, `://127.`, `://[::1]`, or `://redis:` appeared anywhere in the URL.

That is not hostname validation. It is substring validation.

As a result, these remote URLs were incorrectly treated as local plaintext-safe URLs:

- `redis://localhost.attacker.com:6379`
- `redis://localhost@evil.com:6379`
- `redis://127.0.attacker.com:6379`
- `redis://redis:secret@evil.com:6379`

The first and third match because the hostname begins with a local-looking label.
The second and fourth match because the userinfo appears before the real host.

The validator therefore accepted some remote `redis://` URLs that should have required `rediss://`.

This weakened the confidentiality guarantee added in U-03 for vault secrets and the earlier cache protection for investigation data.

---

## Fix design — V-01

### Shared helper

Added a new shared helper:

- `mariana/util/redis_url.py`
- `assert_local_or_tls(url: str | None, *, surface: str) -> None`

This eliminates the drift risk that was already documented in `U03_followup_findings.md`.

### Validation rules

The helper now:

1. Parses the URL with `urllib.parse.urlparse(url)`.
2. Reads `parsed.hostname`, which strips userinfo and isolates the actual host.
3. Rejects the URL if `hostname is None`.
4. Allows `rediss://` for any valid hostname.
5. Allows plaintext `redis://` only when the hostname is exact-local:
   - `localhost`
   - `127.0.0.1`
   - `::1`
   - `redis`
6. Also accepts loopback IP literals via `ipaddress.ip_address(hostname).is_loopback`.
7. Rejects malformed schemes such as non-Redis URLs.

### Call sites updated

The helper now backs both surfaces:

- `mariana/vault/runtime.py`
- `mariana/data/cache.py`

`mariana.vault.runtime._validate_redis_url_for_vault` remains as a thin wrapper for compatibility with existing tests and imports, but it now delegates to the shared helper.

### Security effect

This closes all known bypasses based on substring tricks in host labels or userinfo while preserving the intended local-dev exceptions.

---

## Root cause — V-02

`run_agent_task` fetches per-task vault secrets before normal planning/execution starts.

For `requires_vault=True`, three fail-closed branches existed:

- `except VaultUnavailableError`
- `except ValueError`
- `except Exception` when `requires_vault` is true

Each branch did the following:

1. set `task.state = FAILED`
2. persisted the task
3. returned immediately

The problem was that those returns happened before the function entered the outer `try:` whose `finally:` invokes `_settle_agent_credits`.

That meant the worker-side vault fail-closed path never reached settlement.

Consequences:

- no `agent_settlements` claim row was created
- the reconciler had nothing to retry
- the full reservation remained locked forever

The API-side fail-closed path already refunded correctly. Only the worker path leaked.

---

## Fix design — V-02

### Chosen option

Used Option A.

### Control-flow change

The vault fetch now executes inside the same outer `try:` block that already owns final settlement and vault cleanup.

Implementation details:

- install an initial empty task context before entering the outer `try:`
- perform `fetch_vault_env(...)` inside the outer `try:`
- keep the existing fail-closed behavior of stamping the task `FAILED` and returning
- reset and reinstall context after a successful fetch so the real vault env becomes active
- leave the outer `finally:` unchanged so terminal tasks still pass through `_settle_agent_credits`

### Settlement behavior after fix

When vault fetch fails and `requires_vault=True`:

- task state becomes `FAILED`
- `spent_usd` remains `0.0`
- `_settle_agent_credits` computes `final_tokens = 0`
- `delta = 0 - reserved_credits`
- the full reservation is refunded through the existing idempotent ledger path:
  - `grant_credits`
  - `p_source='refund'`
  - `p_ref_type='agent_task'`
  - `p_ref_id=task.id`

No settlement logic needed to be duplicated.

This keeps the refund path identical to other terminal failures and preserves reconciler semantics.

---

## Code diff summary

### New files

- `mariana/util/__init__.py`
- `mariana/util/redis_url.py`

### Modified files

- `mariana/vault/runtime.py`
  - import shared helper
  - replace substring-match implementation with helper delegation
- `mariana/data/cache.py`
  - import shared helper
  - replace inline substring-match logic with helper call
- `mariana/agent/loop.py`
  - move vault fetch inside the main `try:` path so `finally:` settlement always runs
- `tests/test_v01_v02_vault_hardening.py`
  - new combined regression file
- `loop6_audit/REGISTRY.md`
  - V-01 and V-02 rows marked fixed

---

## Test plan

### New regression file

Added `tests/test_v01_v02_vault_hardening.py` with 10 tests.

#### V-01 coverage

1. `test_substring_bypass_localhost_subdomain_rejected`
2. `test_substring_bypass_userinfo_localhost_rejected`
3. `test_substring_bypass_127_subdomain_rejected`
4. `test_substring_bypass_redis_userinfo_rejected`
5. `test_legitimate_local_still_allowed`
6. `test_legitimate_remote_with_tls_allowed`
7. `test_malformed_url_rejected`
8. `test_data_cache_uses_same_validator`

These pin both the vault validator and the cache client factory to the same hostname-based policy.

#### V-02 coverage

9. `test_worker_vault_fail_refunds_reservation`
10. `test_worker_vault_unexpected_exception_refunds_reservation`

These use local Postgres schema setup plus an HTTP client stub to confirm:

- task ends `FAILED`
- `_settle_agent_credits` is invoked
- refund goes through `grant_credits`
- refunded credits equal the full reservation (`100`)

### Red/green sequence

Confirmed red first on current HEAD before the fix:

- `pytest -x -q tests/test_v01_v02_vault_hardening.py`
- first failing case: `test_substring_bypass_localhost_subdomain_rejected`

After the fix:

- `pytest -x -q tests/test_v01_v02_vault_hardening.py`
- result: 10 passed

### Targeted regressions

Re-ran required and related regressions:

- `tests/test_u03_vault_redis_safety.py`
- `tests/test_t01_marker_loss_no_replay.py`
- `tests/test_s01_rpc_signature_match.py`
- `tests/test_s02_check_constraints.py`
- `tests/test_s03_reconciler.py`
- `tests/test_s04_no_cascade.py`
- `tests/test_u01_stripe_ooo_reversal.py`

Result: 26 passed.

### Full suite

Ran full baseline:

- `pytest -x -q`

Result:

- 381 passed
- 13 skipped

---

## Test count delta

Previous full baseline before this patch: 371 passed, 13 skipped.

New full baseline after this patch: 381 passed, 13 skipped.

Delta: +10 tests.

---

## Residual risk

### Closed by this patch

- substring/userinfo hostname bypasses for vault Redis transport checks
- validator drift between vault and cache
- worker-side stranded reservation on vault fail-closed paths

### Still out of scope

- wider platform Redis client construction outside vault/cache still is not centralized in one shared connection factory
- remote Redis AUTH requirements are still not enforced here
- backup/restore semantics around `requires_vault` remain the same as documented in U-03 follow-up notes

Those are pre-existing follow-up concerns, not blockers for V-01 / V-02 close.

---

## Final state

V-01 and V-02 are fixed on `loop6/zero-bug`.

Mechanically:

- V-01 now validates the actual parsed hostname with a shared helper.
- V-02 now guarantees the worker fail-closed path still runs settlement through the normal idempotent ledger flow.
