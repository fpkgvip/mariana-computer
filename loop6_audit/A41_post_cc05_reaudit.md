# Re-audit #36 — post-CC-04 / CC-05 + Phase F batch 1

**Range audited:** 47af4fe..7f635a9 (CC-04 vault fail-closed, CC-05 reconciler batch_size, Phase F copy)
**Auditor:** adversarial subagent
**Date:** 2026-04-28
**HEAD:** 7f635a9

## Findings

### CC-06 [Severity P2]: empty-dict vault payload (`b'{}'`) bypasses CC-04 fail-closed contract under `requires_vault=True`
- File: mariana/vault/runtime.py:211, 240, 253-268
- Mechanism: CC-04 added three fail-closed branches in `fetch_vault_env`:
  1. `not raw` (line 211) — empty/missing bytes → raise.
  2. `json.loads` failure (line 228-234) — malformed JSON → raise.
  3. `not isinstance(data, dict)` (line 240-245) — non-object → raise.
  4. `_NAME_RE.match(k)` / non-str k/v (line 253-261) — invalid kv shape → raise.
  An attacker-poisoned or operationally-corrupted payload of literally `b'{}'` (or any equivalent empty-object JSON like `b' { } '`) does not trip ANY of these:
    - `not b'{}'` is `False` (the bytes are non-empty), skipping branch 1.
    - `json.loads('{}')` returns `{}`, skipping branch 2.
    - `isinstance({}, dict)` is `True`, skipping branch 3.
    - The for-loop over `data.items()` has zero iterations, so branch 4 is never reached.
  `out` remains the freshly-allocated `{}` and is returned. With `requires_vault=True` the caller in `mariana/agent/loop.py:1179-1185` receives `{}` (no `VaultUnavailableError`), `if vault_env:` at loop.py:1221 is False (no log), and the agent loop proceeds into planning/execution with an empty env. This is the exact U-03 / CC-04 bypass shape the fix was meant to close — only the JSON shape changed from `b'[]'` / `b'"x"'` (now caught) to `b'{}'` (still escapes).
- Reproduction:
  ```python
  class _R: 
      async def get(self, *_): return b'{}'
  await fetch_vault_env(_R(), 'task-1', requires_vault=True, redis_url='redis://localhost')
  # returns {} silently; should raise VaultUnavailableError
  ```
- Test gap: `tests/test_cc04_vault_malformed_payload_fail_closed.py` exercises `b'{'`, `b'[]'`, `b'"just-a-string"'`, `b'null'`, `b'42'`, and bad-kv-shape cases under `requires_vault=True`, but there is no assertion for `b'{}'`. Grep for `b'\{\}'` / `empty_dict` in that file: zero hits.
- Recommended fix: in the `requires_vault=True` branch, treat an empty-dict payload as fail-closed too (a task that registered non-empty `vault_env` cannot legitimately deserialise to `{}` — `store_vault_env` at runtime.py:145 short-circuits `if not env: return`, so the only paths to a stored `{}` are corruption or external poisoning). Add a regression test for `b'{}'`, `b'{ }'`, and `b'  {}\n'`.

### CC-07 [Severity P3]: Product.tsx user-facing copy still contains forbidden hero verb `build` after Phase F sweep
- File: frontend/src/pages/Product.tsx:126
- Mechanism: Phase F batch 1 (commit e8564f7) targeted Product.tsx:87 and Product.tsx:285 but missed line 126: `"whether that's a ten-second lookup or a multi-hour build."` This is *Deft's own* marketing prose (paragraph under the "Deft decides." heading), not a user-quoted example prompt. It is the same category as audit-item §1.11 (`"first build in under a minute"`), which Phase F did fix on OnboardingWizard.tsx:249. The locked rule from PHASE_F_UX_AUDIT.md:13 forbids `build` as a hero verb in user copy; the noun form ("a multi-hour build") is the same lexical violation §1.11 treated as a hard hit. PHASE_F_UX_AUDIT.md does not list this site, and the original audit's `grep` for `build` evidently missed it because of the surrounding hyphenation / preceding word.
- Verification:
  ```
  $ grep -n "multi-hour build" frontend/src/pages/Product.tsx
  126:            whether that's a ten-second lookup or a multi-hour build.
  ```
  This text renders inside a `<p className="...text-muted-foreground">` directly under `<h2>Deft decides.</h2>`, so it is unambiguously user-facing marketing prose, not a code identifier or pipeline label. The Phase F commit message's exclusion list ("`/build` route, `buildId`, `isBuilding`, build/test/deploy pipeline output in mock terminals, trigger keywords for prompt classification") does not cover plain-prose noun usage on a marketing page.
- Recommended fix: rewrite to `"whether that's a ten-second lookup or a multi-hour run."` (same word count, same metre; matches the OnboardingWizard fix vocabulary of "run").

### CC-08 [Severity P4]: 4 admin-auth header validators collapse to identical 500/`Sign-in failed` after Phase F rewrite, erasing the only operator-facing differentiator
- File: mariana/api.py:246-267 (4 raise sites in `_normalize_bearer_auth_header`)
- Mechanism: Pre-Phase-F these four 500 responses had distinct `detail` strings ("admin endpoint called without authorization header" / "authorization header is empty" / "expected Bearer authorization header" / "empty Bearer token"). The function emits no logger calls — the `detail` field was the only signal an operator (or audit log scraper) had to distinguish "no header at all" vs "wrong scheme" vs "Bearer token empty after split." Phase F (commit 39b1d17) collapsed all four to the identical user-facing string `"Sign-in failed. Try again, or contact support if this keeps happening."`, so when an admin endpoint's caller surfaces these errors, ops cannot tell which preflight check failed without re-running the request and stepping through. There are no `logger.warning(...)` / `logger.error(...)` calls added at the four sites to compensate.
- Secondary nit: the new copy reads as a 4xx auth error to the end user, but the status remains 500. A dashboard alert on `5xx_rate` will fire as before (good — no regression in alerting), but a user reading "Sign-in failed. Try again..." with a 500 page may be misled into thinking it is transient when it is actually a misconfigured admin proxy / missing internal credential. Pre-existing 500 status; only the message shifted blame from "internal error" to "user retry."
- Recommended fix: add one `logger.warning("admin_auth_header_rejected", reason=<short_tag>)` per site (`reason="missing"`, `"empty"`, `"wrong_scheme"`, `"empty_token"`) so the operator-facing differentiation that was previously in `detail` survives in structured logs. The user-facing copy can stay as Phase F set it.

### CC-09 [Severity P4]: `fetch_vault_env` accepts empty-string values that `validate_vault_env` would reject — fetch/store contract drift
- File: mariana/vault/runtime.py:103-107 vs 253-268
- Mechanism: `validate_vault_env` (the API ingest path) explicitly drops entries whose value is empty (line 105-107: `if len(value) == 0: continue`). `fetch_vault_env` (the worker read-back path), in contrast, accepts an empty-string value as long as `isinstance(v, str)` — line 254 only rejects on non-str / non-matching key, not on empty value. Net effect: a `b'{"FOO": ""}'` payload (e.g. partial truncation that left a key but lost the value) returns `{"FOO": ""}` instead of `{}` or raising. The agent loop then installs an env with a present-but-empty `FOO`, which is a different observable state than "FOO missing" (e.g. shells that distinguish `[ -z "$FOO" ]` from `[ -v FOO ]`, redactor short-circuits for empty strings, etc.). With `requires_vault=True`, the task proceeds without raising, even though the validate path would never have produced this shape on ingest.
- Pre-existing minor regex issue (also relevant): `_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")` — Python `$` matches before a trailing `\n`, so `_NAME_RE.match("FOO\n")` returns truthy. A poisoned payload `b'{"FOO\\n": "v"}'` passes shape validation. Same regex is used in `validate_vault_env`. Pre-existing, but flagged because CC-04's "shape sanity" branch is the layer that should catch it.
- Recommended fix: either (a) reject empty values in `fetch_vault_env` to match `validate_vault_env`, or (b) drop the empty-value filter from validate_vault_env so the two paths agree. Switch the regex anchor to `\Z` (`re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")`) to close the trailing-newline shape escape.

## Verification of CC-04 (other angles requested in mandate)

The mandate explicitly asked about several escape vectors. Walked each:

- `b''` (empty bytes): `not b''` is True at runtime.py:211 → `if requires_vault: raise VaultUnavailableError(...)`. **Caught.**
- Unicode surrogate halves (e.g. `b'\xed\xa0\x80'`): `raw.decode("utf-8", errors="replace")` at line 222 substitutes U+FFFD, then `json.loads` raises `JSONDecodeError`, then line 232 raises `VaultUnavailableError`. **Caught.**
- Very large payloads: no pre-`json.loads` byte-size cap at line 228 — a multi-megabyte poisoned blob would parse fully before the `_MAX_VAULT_ENV_ENTRIES` slice at line 253 truncates. Pre-existing, not introduced by CC-04. Mild DoS amplifier on a poisoned key but not a fail-closed bypass.
- Boolean keys via `{true: "x"}` JSON: not representable in JSON (RFC 8259 keys are strings). Cannot reach `data.items()`. Non-issue.
- Numeric strings as keys (`{"123": "x"}`): `_NAME_RE.match("123")` fails (must start `[A-Z]`) → with `requires_vault=True` raises at runtime.py:259. **Caught.**
- Top-level JSON `null` (`b'null'`): `data = None`, `not isinstance(None, dict)` True → raise at line 242. **Caught.**

The only unaddressed input is the empty-object case (CC-06 above). Everything else CC-04 was scoped against is correctly handled.

## Verification of CC-05 (other angles requested in mandate)

- `bool` is subclass of `int`: `_parse_reconcile_batch_size` only ever sees `os.getenv(env_var)` which returns `str | None`. `int(raw)` on a string can never produce a `bool`. Static call sites pass `default=50` (int literal). The function-entry guards (`if batch_size <= 0`) at settlement_reconciler.py:89 and research_settlement_reconciler.py:57 don't reject `bool` (`True <= 0` is `False`, so `True` slips through to PG's `LIMIT $2`), but no internal caller passes `bool`, and asyncpg would surface it as a type error before SQL execution — daemon would log+sleep+retry (same outer behaviour as a transient PG error, not the silent-claim regression CC-05 fixed). Defensive nit, not a bug.
- Float strings (`"3.14"`): `int("3.14")` raises `ValueError` → caught at line 1209 → falls back to `default` with a `settlement_reconciler_batch_size_unparseable` warning. **Correct.**
- Whitespace strings (`" 5 "`): `int(" 5 ")` returns 5 (Python strips). **Correct.**
- Negative int strings (`"-5"`): `int("-5")` → -5 → `max(1, -5)` → 1, `source="clamped"`. **Correct.**
- `"0"`: `parsed=0`, `clamped=1`, `source="clamped"`. **Correct.**
- Hex / scientific (`"0x10"`, `"5e2"`): both raise `ValueError` → fallback. **Correct.**

The CC-05 helper is clean. No escape found.

## Verification of Phase F (other angles requested in mandate)

- New copy strings introduce zero forbidden hero verbs (`build`, `ship`, `supercharge`, `empower`, `unlock`, `transform`, `accelerate`, `revolutionize`, `reimagine`) — verified by inspecting all 22 rewrites in commit 39b1d17 and the 5 toast/Chat sites in e9a9ca6. New phrases ("Sign in to continue.", "Lost connection. Reload to reconnect.", "Could not load your conversations. Try again.", etc.) are clean.
- HTTP status codes preserved exactly across all 22 rewrites (4× 500 admin auth, 1× 503 db, 1× 503 config, 5× 503 / 401 supabase auth, 4× 401 bearer, 6× 500 CRUD, 1× 500 investigation, 4× 502 payments). No semantic regression.
- The 6 conversation/message CRUD rewrites at api.py:2451-2724 keep `logger.error(...)` calls with the original Supabase status/body, so server-side observability is intact even where user-facing detail was simplified.
- The Phase F audit (PHASE_F_UX_AUDIT.md §1.x) explicitly excluded user-quoted example prompts (Index.tsx:21, Research.tsx:52,101,121, OnboardingWizard.tsx:69,71). I did NOT count those as findings — but Product.tsx:126 (CC-07 above) is *not* a user-quoted example prompt and was missed.
- "Sign-in failed. Try again, or contact support if this keeps happening." is reused for 5 distinct sites (4 admin-header validators + the investigation-submit error). The investigation-submit variant is on a 500 with proper logger.error; the 4 admin sites have no logger (CC-08 above).

## Verdict

4 findings (1 × P2, 1 × P3, 2 × P4). Streak resets.

| ID | Severity | Surface |
|---|---|---|
| CC-06 | P2 | Empty-dict `b'{}'` payload bypasses vault fail-closed |
| CC-07 | P3 | `Product.tsx:126` "multi-hour build" forbidden-verb miss |
| CC-08 | P4 | Admin-auth differentiation lost, no compensating log |
| CC-09 | P4 | `fetch_vault_env` accepts empty values that `validate_vault_env` rejects; `_NAME_RE` `$` allows trailing-newline keys |
