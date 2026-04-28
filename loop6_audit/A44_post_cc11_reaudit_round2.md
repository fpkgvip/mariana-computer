# A44 — Post-CC-11 re-audit (round 2/3, opus)

**Repo:** `mariana`
**Branch:** `loop6/zero-bug`
**HEAD audited:** `465c5ee`
**Cumulative range:** `c108b1e..465c5ee` (Phase D + Phase F + CC-04..11 work since A39)
**Round-1 reference:** `A43_post_cc11_reaudit.md` (gpt, clean)
**Auditor:** opus, hostile mode

## Verdict (one-line)

**Two new findings (both pre-existing, surfaced by the round-2 mandate's Item 3 — CI workflow SHA pinning).** No new findings inside the CC-04..CC-11 fix surface itself; the round-1 conclusion holds for that scope.

The CI/deploy supply-chain finding is technically pre-existing (the workflows landed in commit `38bbf3a` and `47af4fe` in Phase D, and `deploy.yml` predates Phase D). The mandate explicitly named CI workflow review (Item 3) as in-scope for this round, so I am surfacing them.

## Findings

### F-A44-01 — `appleboy/ssh-action@v1` is a third-party action with deploy creds, pinned to a floating tag — **P2**

- **File:line:** `.github/workflows/deploy.yml:18`
- **Code:**
  ```yaml
  - name: Deploy via SSH
    uses: appleboy/ssh-action@v1
    with:
      host: ${{ secrets.HETZNER_HOST }}
      username: ${{ secrets.HETZNER_USER }}
      key: ${{ secrets.HETZNER_SSH_KEY }}
      script: |
        ...
  ```
- **Class:** Supply-chain / CI hardening.
- **Description:** `appleboy/ssh-action` is a third-party Marketplace action. The reference `@v1` is a floating major-version git tag, not a commit SHA. If the upstream maintainer's account is compromised (credential theft, repo takeover, malicious PR merged into the tag), or the tag is force-moved, the next push to `master` runs attacker-controlled code in the deploy job with **the production SSH private key (`HETZNER_SSH_KEY`) and host (`HETZNER_HOST`) materialised as environment variables**. The script runs `git reset --hard origin/master` followed by `docker compose build --no-cache mariana-api mariana-orchestrator` and `docker compose up -d --force-recreate` against `/opt/mariana` — full code-execution-as-deploy on the production box. There are no integrity controls on the action body between the maintainer and our deploy.
- **Why this matters now / wasn't caught before:**
  - Prior re-audits (A30..A43) focused on the application surface, not the CI supply chain.
  - The round-2 mandate explicitly names this surface (Item 3): "are GitHub Actions versions pinned to SHA (not floating tags)?"
  - Industry baseline (OpenSSF Scorecard, GitHub's own hardening guide, CISA SSDF) is to pin third-party actions by full 40-char SHA. First-party `actions/*` is a much lower-risk concession; third-party with deploy secrets is the canonical case where SHA pinning is mandatory.
- **Reproduction:** No proof-of-concept needed — the threat model is the upstream-maintainer-compromise class. `git ls-remote https://github.com/appleboy/ssh-action.git refs/tags/v1` resolves to whatever the maintainer has set the tag to at deploy time.
- **Fix sketch (discovery only — do not apply):** Replace `appleboy/ssh-action@v1` with `appleboy/ssh-action@<full-sha>` (look up the current `v1.x.x` SHA, pin it, add a comment `# v1.0.x — review SHA before bumping`).  Optionally add `permissions: {}` at the workflow root to deny extra `GITHUB_TOKEN` scopes the action might request.

### F-A44-02 — Workflows have no top-level `permissions:` block (default `GITHUB_TOKEN` scope) — **P3**

- **File:line:** `.github/workflows/ci.yml` (no `permissions:` block); `.github/workflows/deploy.yml` (no `permissions:` block).
- **Class:** CI hardening / least-privilege.
- **Description:** Neither workflow declares a `permissions:` block, so every job inherits the repo-default `GITHUB_TOKEN` permissions. For repos created before 2023-02 (or with the org default still set to "permissive"), that default is `contents: write, pull-requests: write, ...`. Combined with F-A44-01's floating-tag third-party action, a compromised `appleboy/ssh-action@v1` could not only steal the SSH key but also push commits, open PRs, or modify branch protections via `GITHUB_TOKEN` if the org default is still permissive. The deploy workflow needs only `contents: read`; CI needs only `contents: read`.
- **Reproduction / discovery:** `grep -nE "^permissions:|^  permissions:" .github/workflows/*.yml` returns no matches. Confirmed absent.
- **Why this is P3 rather than P2:** The actual `GITHUB_TOKEN` scope depends on the **repo / org default**, which I can't inspect from the repo alone. If the org default is already `read`, this is moot. If it's `write`, this materially widens the F-A44-01 blast radius. Worth fixing regardless; cheap defence-in-depth.
- **Fix sketch (discovery only — do not apply):** Add at the top of each workflow file:
  ```yaml
  permissions:
    contents: read
  ```

## What I checked and did NOT find a new bug in (paranoid breadcrumbs)

The following were the round-2 mandate's explicit focus areas. I did the work and came up empty.

### 1) CC-04 / CC-06 — other vault callers besides `fetch_vault_env`?

Grep across `mariana/` shows the only non-test callers of vault runtime functions are:

- `mariana/agent/api_routes.py:437-438, 511, 573-580` — start_agent_task: `validate_vault_env` (pre-insert), `store_vault_env` (post-insert, pre-enqueue, fail-closed via VaultUnavailableError + 503).
- `mariana/agent/loop.py:44-46, 1179-1184, 1490` — run_agent_task: `fetch_vault_env(requires_vault=...)` and `clear_vault_env`. Fail-closed early-return is wrapped in the outer try/finally that always resets ctx_handle and clears Redis (loop.py:1421, 1485-1492).

There are **no other call sites**. CC-04/06 fail-closed semantics are the canonical implementation; no parallel reader exists that would bypass them.

(`mariana/vault/store.py` is the **persistent** Supabase-backed secret store, separate from the **per-task ephemeral** Redis vault_env. The two surfaces share the `_NAME_RE` shape but are otherwise independent — a CC-04..06 issue at one cannot be reached via the other.)

### 2) CC-08 timing-channel from per-branch `logger.warning` calls

The branch-distinguishing log lines are emitted **after** the HTTPException is constructed but before `raise`, all four with similar work (string formatting + structlog dispatch). The user-facing detail is identical across all four branches. The only differential is in operator-side logs.

A remote attacker has no observation channel here: the response body is identical, the response status is 500 across all four branches, and there is no client-observable timing difference of useful magnitude (sub-microsecond difference in log dispatch is far below any cross-Internet jitter floor). An attacker with **log read** would already be outside the threat model this fix addresses (they are inside the operator boundary). Not a P-class bug; the round-1 conclusion holds.

(I also checked: `_normalize_bearer_auth_header` is invoked from admin Supabase pass-through endpoints, which are gated by the same JWT middleware — only authenticated callers can trigger any of the four branches.)

### 3) CC-09 / CC-10 regex migration concern (data accepted on write, rejected on read)

I traced every regex changed in `ca8d6bf` and `80a3cb4`:

- **`mariana/vault/store.py:138`** — `_NAME_RE` is called by `_validate_name()` only on `create_secret` (write path). Read paths (`list_secrets`, `_secret_from_row`, `update_secret`) never re-validate. Existing rows with `"FOO\n"`-style names cannot exist in practice because the **router-layer `CreateSecretRequest.name`** already validates with `pattern=r"^[A-Z][A-Z0-9_]{0,63}$"` (router.py:129), and pydantic v2's default rust-regex engine **does not** match a trailing newline (verified in repl: `M(name='FOO\n')` → `ValidationError`). So no migration risk: the only theoretical poisoned name would have to predate pydantic v2 + the router constraint, which never existed in this repo. Confirmed clean.

- **`mariana/api.py:1671`** — `_SAFE_PREVIEW_TASK = r"^[A-Za-z0-9_\-]{1,64}\Z"`. Used to gate preview routes against on-disk preview directories. Task IDs are `str(uuid.uuid4())` (api_routes.py:431), length 36, hex+hyphen — all match comfortably. The `\Z` change cannot reject a legitimate live task ID. Existing on-disk preview directories named with poisoned IDs would now 404 on read, but such directories cannot have been created via the legitimate write path (the write path is the same `_SAFE_PREVIEW_TASK` validator at api.py:1746). Confirmed clean.

- **`mariana/connectors/sec_edgar_connector.py:236`** — `r"^([a-z0-9-]+\.)*sec\.gov\Z"`. `urlparse(url).hostname` lowercases input; `WWW.SEC.GOV` → `www.sec.gov` → matches. Trailing-dot hosts (`www.sec.gov.`) were already rejected before the `\Z` change because the pre-`\Z` regex's `$` did not match before a trailing `.`. Confirmed clean.

- **`mariana/tools/memory.py:33`** — `_USER_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}\Z"`. user_ids come from authenticated JWT (`current_user["user_id"]`), which is a Supabase UUID — passes the regex. Existing on-disk memory directories under poisoned user_ids cannot have been created legitimately. Confirmed clean.

- **`sandbox_server/app.py:101-102, 211`** — `_SAFE_ID_RE`, `_PATH_COMPONENT_RE`, env-name regex. user_id is forwarded from API server → sandbox; only the API can call sandbox; API user_ids are JWT-validated UUIDs. Path components flow from server-controlled tool calls. Env names flow from server-controlled vault_env (already constrained by `_NAME_RE` at validate_vault_env). Confirmed clean.

### 4) CC-11 oversize-value migration concern — pre-existing oversize secrets in production?

The vault_env oversize cap is `_MAX_VAULT_VALUE_LEN = 16_384` characters (vault/runtime.py:60). The persistent secret store enforces a different cap: `value_blob` ≤ 65552 bytes encrypted (store.py:367). The frontend `encryptSecret` enforces 16384 plaintext chars (`vaultCrypto.ts:288`).

I traced the git history of `vaultCrypto.ts`: the 16384-char limit has been present since the file was first written; it has never been weakened. So a secret in the persistent store CANNOT have been legitimately created with plaintext > 16384 chars via any version of this repo's frontend. Therefore there are no pre-existing oversize secrets to worry about; CC-11 is a defence-in-depth fix for an impossible-via-honest-flow shape.

(There is a defence-in-depth gap I noted but did not promote to a finding: server-side `_validate_lengths(blob_max=65552)` in store.py would happily accept an encrypted blob whose plaintext is 50k chars if a non-frontend client encrypted directly. CC-11 closes the matching read-side gap on the **vault_env / Redis** surface but not on the **vault_secrets / DB** surface. The DB surface decryption is purely client-side, so the server never observes plaintext, and the corresponding `vault_env` POST would be rejected by `validate_vault_env` (write path) before it could reach Redis. So even with a hypothetical large-plaintext DB row, the worker can never see >16k via the legitimate flow. Not a finding; documented for completeness.)

There is **no startup check** that warns operators about pre-existing oversize Redis vault_env blobs. I did not find this in `mariana/main.py` lifespan / startup. Given that:

  (a) Redis vault_env blobs have a TTL (typically `int(body.max_duration_hours * 3600) + 300`, max ~24h+), so any legacy blob naturally evicts;
  (b) the only legitimate writer is `store_vault_env`, which goes through `validate_vault_env`, which rejects > 16384;

…the operational risk of "legitimate oversize blob silently breaks tasks post-deploy" is functionally zero. Not a finding.

### 5) Concurrency holes (asyncio.gather / pipelines / txns / heartbeat)

- **`asyncio.gather`** call sites with `return_exceptions=True` are: api.py:8573 (health probes — exceptions inspected), polygon_connector.py:333 (per-key error inspection), unusual_whales_connector.py:265 (same). `perspectives.py:243` uses `return_exceptions=False` because each `run_perspective` already swallows internally → returns None. All correct usage.
- **Redis pipelines**: only one in `mariana/data/cache.py:303`, used with `transaction=True` (MULTI/EXEC), atomic.
- **asyncpg transactions** are used at the right boundaries (api.py:3969, branch_manager.py:435/522, event_loop.py:1313/1449/2450/2673, etc.). I did not find a write multi-step that should be transactional but isn't.
- **Heartbeat / TTL races**: settlement reconciler (settlement_reconciler.py:120-137) uses the CC-02 materialised CTE form with `FOR UPDATE SKIP LOCKED`, atomic claim via `UPDATE … RETURNING`. Symmetric in `research_settlement_reconciler.py:72-90`. Confirmed correct.
- **`vault/runtime.py:set_task_context` reset/set ordering** (loop.py:1219-1220) runs sequentially in the same async task — no concurrent observer between reset and re-set. Outer try/finally always resets `ctx_handle` and clears Redis even on the fail-closed early-return paths (verified at 1421 / 1485-1492). Confirmed clean.
- **Pre-existing observation (NOT a new finding):** `mariana/main.py:1090-1109` requeues stuck tasks (no heartbeat for 60 s) by RPUSH'ing to `agent:queue` without bumping the task row's `updated_at` or taking a per-task idempotency lock. If the orchestrator restarts faster than 60 s after a successful single requeue cycle, the same task is pushed twice. The `_run_one` semaphore caps process-local concurrency but does not prevent a single task being claimed twice if there are multiple orchestrator processes (multi-host deployment). Loop6 deploy is single-orchestrator (Hetzner), so not exploitable today. Pre-existing, out-of-scope for CC-04..CC-11.

### 6) SQL injection — f-string interpolation

Three sites flagged by grep:

- **`mariana/api.py:3643`** — `f"DELETE FROM {table} WHERE task_id = $1"` — `table` is a literal element of the closed list `cascade_tables` defined immediately above (api.py:~3625). Not user-controlled. ✓
- **`mariana/data/db.py:853, 1211`** — `f"UPDATE {tbl} SET {set_clauses} WHERE id = $1"` — column names come from `_ALLOWED_TASK_COLUMNS` / `_ALLOWED_BRANCH_COLUMNS` allowlist with hard `assert` (db.py:846, 1204). Values are bound as `$N` placeholders. ✓
- **`mariana/agent/api_routes.py:294, 297`** — string-concat SQL using `where = "user_id = $1"` plus `f" ORDER BY ... LIMIT {lim_placeholder} OFFSET {off_placeholder}"` where the placeholder names are derived from `len(params)` (an int). All values bound by position. ✓

No injection vector.

### 7) Forbidden language regrep in `frontend/src/`

- `scrape` / `crawl` / `spider` regrep returns one hit at `frontend/src/lib/pageHead.ts:11` — a code comment about social-card crawlers (Facebook/Twitter), not user-visible copy. Acceptable per the audit's user-copy scope.
- Hero-verb regrep (`unleash|supercharge|empower|revolutionize|seamless|game-chang|cutting-edge|harness|transform your`) returns one hit on `Product.tsx:270` ("test harnesses" — noun, not the marketing verb).
- Emoji regrep returns three `✓` (U+2713, dingbats) used as step-completion indicators in `AgentPlanCard.tsx:83`, `AgentTaskView.tsx:164`, `PreviewPane.tsx:390`. Functionally icon glyphs; the Phase F batch 1 commit explicitly removed `⚠️` warning emojis but did not flag `✓` step indicators. Out of scope.
- I noted that `Product.tsx` retains conjugated forms (`builds`, `building`, `built`, `ships`, `Shipping`) at lines 66/158/163/175/234/241/248/253. The Phase F audit (`PHASE_F_UX_AUDIT.md` §1.6, §1.7) flagged ONLY `Product.tsx:87` and `Product.tsx:285`, both of which were fixed in commit `e8564f7`. The retained conjugations are out of the audit's documented scope. **Not a finding** — the round-1 auditor was correct to skip this.

### 8) Migration count / baseline freshness

- `frontend/supabase/migrations/` contains 24 forward migrations (001-022 + 004b + 024). Numbering jumps 022 → 024 with no `023_*.sql` ever committed (`git log --all --diff-filter=A` confirms nothing was ever added under that prefix). This is a numbering gap, not a missing migration. The pair-check script (`.github/scripts/check_migration_pairs.sh`) verifies pair existence per forward migration, not sequential numbering — correctly does not flag this.
- The CI baseline `.github/scripts/ci_full_baseline.sql` reflects the CC-09 baseline and the 024 (`refund_credits` aggregate ledger) shape (`grep -n "aggregate" ci_full_baseline.sql` → line 988 `'aggregate', true` matches the migration body). Baseline is fresh.

### 9) `_BYTEA_HEX_RE` left as `$` (round-1 review)

I re-verified the round-1 reasoning. `_BYTEA_HEX_RE = re.compile(r"^\\x([0-9a-fA-F]*)$")` at `mariana/vault/store.py:63` accepts `\\xdeadbeef\n` because Python `$` matches before a final newline. The capture group does not include the newline. `bytes.fromhex("deadbeef")` succeeds. A trailing newline does not produce a different decoded byte payload. The PostgREST `bytea` transport is server-server (Supabase REST), not user-controlled. Confirmed acceptable; no migration concern.

### 10) Pydantic regex behavior verification (anti-regression)

I executed a quick repl check (verified in this audit run) to confirm pydantic v2's default regex engine is strict on `$`:

```
>>> M(name='FOO\n')
ValidationError: String should match pattern '^[A-Z][A-Z0-9_]{0,63}$'
```

So the **router-layer** validators using bare `$` (vault/router.py:129, api.py:741/743, browser_server/app.py:347/366/384/400) are still strict in this stack. CC-09/CC-10 fixed only the **runtime-layer** `re.match` calls where Python regex semantics governed. Defense-in-depth preserved.

## Bottom line

- **Two findings, both about CI/CD supply-chain hardening (deploy.yml + ci.yml).** Both surfaced from the round-2 mandate's explicit Item 3. They are pre-existing relative to CC-04..CC-11, but the mandate scope ("entire Phase D + Phase F + CC-04..11 work since prior convergence at A39") includes them since the workflow files were last touched inside this range (38bbf3a, 47af4fe).
- **No new findings inside the CC-04..CC-11 fix surface.** I cross-checked each commit, traced every changed regex / branch / contract drift, looked for second-order bugs introduced by the fixes themselves, and read the surrounding code paths for drift. The round-1 (gpt) conclusion holds for that scope.

## Streak verdict

**`reset`** — F-A44-01 (P2) plus F-A44-02 (P3) are real findings within the cumulative range the mandate names. Even if both are pre-existing, the mandate explicitly includes Item 3 ("CI workflow itself …") in this round's scope, so I cannot in good conscience call this round clean.

If the parent agent decides F-A44-01/02 are out-of-scope (because the workflows weren't materially modified by CC-04..11), the within-CC-scope verdict is **clean** and the streak stands at 2/3.

## Confidence call

**I do NOT trust the codebase as production-ready right now**, narrowly because of F-A44-01.

The application surface (CC-04..11 fixes, vault contract, settlement reconciler, agent loop, regex hardening, frontend copy) is in good shape — Phase D / Phase F / CC-04..11 have been thorough, the test count (509/13/0 + 144 vitest) is healthy, and I could not find a new application-level bug. Round-1's clean read on the application is justified.

But a deploy workflow with `appleboy/ssh-action@v1` + production SSH credentials is a single-supply-chain-incident-away from full prod compromise. The fix is mechanical (resolve to SHA, paste, commit) and should land before this codebase is treated as "shipping-ready in a hostile environment."
