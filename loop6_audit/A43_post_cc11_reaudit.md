# A43 — Post-CC-11 re-audit

**Repo:** `mariana`
**Branch:** `loop6/zero-bug`
**HEAD audited:** `cf56714`
**Primary range:** `6be5860..cf56714`
**Holistic diff spot-check:** `7f635a9..cf56714`

## Verdict

No new P1-P4 bug found in the audited range.

## Findings

- **No new findings.**

## Audit notes by mandate

### 1) CC-10 regex hardening: did `\Z` reject previously valid inputs?

I traced every regex changed in `80a3cb4`:

- `mariana/tools/memory.py` — `_USER_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}\Z"`
- `sandbox_server/app.py` — `_SAFE_ID_RE`, `_PATH_COMPONENT_RE`, and the inline env-name regex in `ExecRequest._check_env`
- `mariana/api.py` — `_SAFE_PREVIEW_TASK = r"^[A-Za-z0-9_\-]{1,64}\Z"`
- `mariana/connectors/sec_edgar_connector.py` — `r"^([a-z0-9-]+\.)*sec\.gov\Z"`

Caller tracing / regression check:

- `UserMemory(user_id, ...)` uses `_USER_ID_RE` only to gate the on-disk memory path. `\Z` changes only trailing-newline behavior; normal IDs are unchanged.
- Sandbox `ExecRequest.user_id`, `_safe_workspace_path()` path components, and env-name validation all still admit the same ordinary inputs; only newline-suffixed strings are newly rejected.
- Preview task IDs are generated as UUID strings in `mariana/agent/api_routes.py` (`str(uuid.uuid4())`), which still fit `_SAFE_PREVIEW_TASK` comfortably.
- Preview-serving code in `mariana/api.py` uses the validated `task_id` only for preview dir lookup and cookie-path scoping; UUID-like current task IDs remain valid.
- `sec_edgar_connector.get_filing_text()` validates `urlparse(url).hostname`; Python lowercases ordinary mixed-case hosts, so valid SEC hosts such as `WWW.SEC.GOV` still normalize to `www.sec.gov` and pass. A trailing-dot host like `www.sec.gov.` was already rejected before the `\Z` change, so this commit did not newly break that edge case.

Conclusion: I did not find a legitimate currently-used input shape that was accepted before and is now incorrectly rejected.

### 2) CC-11 oversize-value handling and multibyte UTF-8

I specifically checked for byte-vs-character drift.

Relevant behavior in `mariana/vault/runtime.py`:

- `validate_vault_env()` rejects values with `len(value) > _MAX_VAULT_VALUE_LEN`
- `store_vault_env()` persists `json.dumps(dict(env))`
- `fetch_vault_env()` now fail-closes / drops on `len(v) > _MAX_VAULT_VALUE_LEN`

Important detail: both the write path and the read path use Python `len(str)`, i.e. character count, not UTF-8 byte count. They are therefore internally consistent.

Also, `store_vault_env()` uses plain `json.dumps(...)` with default `ensure_ascii=True`, so non-ASCII characters are serialized into ASCII `\uXXXX` escapes before being stored in Redis. That means Redis byte size can exceed the character count for multibyte content, but the application contract on both sides is still character-count based, not byte-count based.

So I do **not** see a new CC-11 contract drift between store and fetch for multibyte text. The cap is semantically “16384 characters,” not “16384 UTF-8 bytes.”

### 3) `sec_edgar_connector` and `_SAFE_PREVIEW_TASK` legitimacy check

- `_SAFE_PREVIEW_TASK` still accepts all current task IDs in this codebase’s normal flow (UUIDs with hyphens).
- The SEC host regex still accepts `sec.gov` and subdomains, and `urlparse(...).hostname` lowercases mixed-case host input before regex matching.
- I found no legitimate current task ID or SEC host form that this patch newly rejects.

### 4) `_BYTEA_HEX_RE` left as `$`

`mariana/vault/store.py` still has:

```python
_BYTEA_HEX_RE = re.compile(r"^\\x([0-9a-fA-F]*)$")
```

Because Python `$` matches before a final newline, `_BYTEA_HEX_RE.match("\\xdeadbeef\n")` succeeds and `bytes.fromhex("deadbeef")` decodes successfully.

I checked downstream impact:

- `_from_bytea()` immediately decodes the captured hex and returns raw bytes.
- The trailing newline is not preserved in the returned bytes; it is effectively ignored by the regex anchor behavior.
- If the hex is malformed (`\\xZZ\n`, odd-length invalid hex, etc.), `_from_bytea()` still raises `VaultError`.

So the auditor’s “leave as `$`” reasoning looks acceptable for this surface:

- it is a PostgREST `bytea` transport parser, not a user-identifier validator,
- a trailing newline does not create a different decoded byte payload,
- and a malicious DB response capable of changing these fields is already a stronger compromise than this parser nuance.

I do **not** see a practical new P1-P4 issue here.

### 5) Holistic diff review: `7f635a9..cf56714`

Files changed in the cumulative diff are narrow and match the intended fixes:

- vault runtime oversize fail-closed behavior
- regex anchor hardening in memory / sandbox / preview / SEC host validation
- regression tests for CC-06 / CC-09 / CC-10 / CC-11
- frontend loading-state files from Phase F batch 2

I did not find a cross-file regression introduced by the cumulative set.

### 6) Migrations vs `.github/scripts/ci_full_baseline.sql`

Migration directory tops out at `024_bb01_refund_credits_aggregate_ledger.sql` plus its revert. I found **no migrations newer than 024**, so there is nothing new that needs to be reflected in `.github/scripts/ci_full_baseline.sql` on this specific check.

### 7) Frontend forbidden words in Phase F batch 2 loading copy

I checked the newly added loading/skeleton copy in:

- `frontend/src/components/deft/vault/VaultSkeleton.tsx`
- `frontend/src/pages/Vault.tsx`
- `frontend/src/pages/Chat.tsx`
- `frontend/src/pages/Admin.tsx`

User-visible strings added in this batch are essentially:

- `Loading vault`
- `Loading conversation`
- `Loading…`
- `Loading admin console`

I did not find newly introduced forbidden words from the Phase F audit list (`scrape`, `crawl`, hero-verb marketing copy, emojis, etc.) in these added loading states.

## Bottom line

Re-audit result: **no new P1-P4 issue found** in `6be5860..cf56714`.
