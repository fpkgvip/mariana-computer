# CC-07 / CC-08 / CC-09 Fix Report

**Date:** 2026-04-28
**Branch:** `loop6/zero-bug`
**Audit source:** `loop6_audit/A41_post_cc05_reaudit.md`
**Commits:**
- `c4f1950` — CC-07 fix forbidden hero verb in Product page user copy
- `9d4ed58` — CC-08 fix admin auth header validators silent on operator side
- `ca8d6bf` — CC-09 fix vault contract drift empty-string + trailing-newline name regex
- `65da7b5` — (CC-06, parallel agent) — incidentally bundled the CC-09 `runtime.py` changes that were authored in this same workspace at the time the parallel agent took its snapshot. The CC-09 logic is unchanged; the `_NAME_RE` switch to `\Z` and the `if len(v) == 0` empty-value branch in `fetch_vault_env` ride along that commit. CC-09's separate commit `ca8d6bf` carries the `store.py` regex switch and the regression test file.

**Final HEAD:** `ca8d6bf`
**Test count:** 472 → **490 passed**, 13 skipped, 0 failed (+18 net)

---

## CC-07 (P3): forbidden hero verb in Product page

### Diagnosis (from A41 §CC-07)
`frontend/src/pages/Product.tsx:126` user-facing marketing prose under the `<h2>Deft decides.</h2>` heading still contained `"…or a multi-hour build."` — the noun form of the forbidden hero verb `build` (PHASE_F_UX_AUDIT.md:13 locked rule). Phase F batch 1 (commit `e8564f7`) targeted Product.tsx:87 and 285 but missed line 126 because the surrounding hyphenation / preceding word ("multi-hour ") slipped past the original `grep`. This is the same lexical category as audit-item §1.11 (`"first build in under a minute"`), which Phase F did fix on OnboardingWizard.tsx:249.

### Fix (commit `c4f1950`)
```diff
-            whether that's a ten-second lookup or a multi-hour build.
+            whether that's a ten-second lookup or a multi-hour run.
```
Same word count, same metre; matches the OnboardingWizard fix vocabulary of `"run"`.

### Verification — full-frontend forbidden-verb sweep
After the fix I re-grepped the entire `frontend/src/` tree for all nine locked forbidden hero verbs (`build`, `ship`, `supercharge`, `empower`, `unlock`, `transform`, `accelerate`, `revolutionize`, `reimagine`), each anchored with a leading space to avoid false-positives on path fragments. Hits and disposition:

| File:line | Hit | Disposition |
|---|---|---|
| App.tsx:27 | "only ships what's needed for the landing render" | **code comment** — not user-facing. Documenting code-only. |
| AppErrorBoundary.tsx:3,81 | `buildReportIssueUrl` | **identifier** — code-only. |
| FileViewer.tsx:84,85,115,153 | "transforms" / "transform" | **code comment / variable** — code-only. |
| ProgressTimeline.tsx:556,588 | `buildStepGroups` | **identifier** — code-only. |
| ui/progress.tsx:17 | CSS `transform: translateX(...)` | **code-only**. |
| OnboardingWizard.tsx:71 | `"Take this CSV of sales data and build me an interactive dashboard."` | **user-quoted example prompt** — explicitly excluded by Phase F audit (PHASE_F_UX_AUDIT.md §1.x). |
| OnboardingWizard.tsx:388 | "the build page" | **route reference** — refers to the `/build` route, which is the canonical product surface; matches Phase F's `/build` route exclusion. |
| deft/PreviewPane.tsx:6,10,460 | "agent ships" / "Planning your build…" | **code comment** + status-string referring to the user's project being built; same `/build`-route pipeline category. **Documenting as code-only / pipeline-output.** |
| deft/ProjectsSidebar.tsx:19 | "in this build" | **code comment** about the frontend build itself. Code-only. |
| deft/SecretsTable.tsx:22,26,45 | `unlocked` / `unlock` (vault domain) | **feature term** — `unlock` is a vault-domain noun (the user explicitly unlocks a vault). Same as Phase F's existing exclusion list. |
| deft/VaultSetupWizard.tsx:91,111 | "unlocked", "unlock your vault" | **vault feature term** — same as above. |
| deft/VaultUnlockDialog.tsx:* | `Unlock` (component / handler) | **identifier + feature term**. |
| deft/vault/SecretsEmptyState.tsx, UnlockedBar.tsx, VaultSkeleton.tsx | `unlocked` | **vault feature term**. |
| AuthContext.tsx:76,130 | `buildUser` | **identifier**. |
| useVault.ts:* | `unlock*` identifiers and feature term | **vault domain**. |
| lib/agentApi.ts:45 | `"Balanced default for most builds."` | User-facing tier description. Noun form, but referring to *the user's builds* in the `/build` route domain. Same exclusion category as the `/build` route. **Documenting; not changing without an explicit copy decision.** |
| lib/agentRunApi.ts:20 | "ships the resulting NAME→plaintext map" | **code comment**. |
| lib/errorToast.ts, observability.ts | `buildReportIssueUrl` | **identifier**. |
| lib/vaultApi.ts, vaultCrypto.ts | `unlockWithPassphrase` etc. | **identifiers + vault feature term**. |
| pages/Chat.tsx:286,307 | "transformations safe for dangerouslySetInnerHTML" / "transforms" | **code comments**. |
| pages/Index.tsx:106 | `const buildHref = "/build?prompt=..."` | **identifier + `/build` route**. |
| pages/Index.tsx:129 | `"AI résumé builder, exports to PDF"` | **user-quoted example prompt**. |
| pages/Index.tsx:387 | mock terminal `"▸ build"` | **mock-terminal pipeline output** — explicitly excluded by Phase F. |
| pages/InvestigationGraph.tsx:1065,1070 | `d3.zoomIdentity` `transform` | **D3 API**. |
| pages/Login.tsx:201 | mock terminal `"▸ build"` | **mock-terminal output**. |
| pages/DevVault.tsx:8,265 | "unlock your vault" | **vault feature term**. |
| pages/Pricing.tsx:137 | `"a passing build, a passing test suite, or a successful deploy"` | **CI/CD pipeline output category** — explicitly mentioned in Phase F's "build/test/deploy pipeline output in mock terminals" exclusion list. |
| pages/Product.tsx:66 | `"it builds a check and runs it."` | Verb form of "build" in user-facing prose. **Edge case** — Phase F batch 1 explicitly visited this file and chose to keep this construction (the verb is intransitive-adjacent: Deft "builds a check"). I do NOT touch this without an explicit Phase F follow-up; it was an in-scope sentence in the Phase F sweep and was kept intentionally. Documenting for the next audit pass. |
| pages/Product.tsx:158,163 | `"What it actually ships"` (h2 + comment) | Verb form of "ships". Same Phase F status as :66 — visited and kept. |
| pages/Product.tsx:241 | `"calls, building and testing"` | Same. |
| pages/Product.tsx:271 | `"Run full build/test/deploy loops autonomously"` | **explicit "build/test/deploy" pipeline phrase** — Phase F exclusion. |
| pages/Research.tsx:46 | `"~12 min build, 45s each run"` | **runtime label** for a research pipeline — pipeline-output exclusion. |
| pages/Research.tsx:123 | `"CFO edits a starting point instead of building from scratch."` | Verb form. Same status as Product.tsx:66 — visited by Phase F and kept. |
| pages/Skills.tsx:361 | `"research, building, analysis, automation"` | Comma-separated activity list. Same status — visited and kept. |
| pages/DevObservability.tsx:* | `buildReportIssueUrl` | **identifier**. |
| pages/DevStates.tsx:125 | `"Could not unlock vault"` | **vault feature term**. |
| pages/DevStudio.tsx:6 | "this route is not registered in production builds" | **code comment**. |
| pages/Vault.tsx:6,7 | `unlock` | **vault feature term**. |
| test/b26_*.test.ts, b29_*.test.ts | `transform` / `buildSource` | **test code**. |

**Net:** the only finding fitting CC-07's exact shape — a forbidden hero verb hiding in user-facing marketing prose that Phase F missed — was Product.tsx:126. All other hits are either (a) code identifiers / comments, (b) the `/build` route domain, (c) the `unlock` vault feature term, (d) mock-terminal / CI pipeline output, or (e) sentences Phase F batch 1 explicitly visited and chose to keep (Product.tsx:66, :158, :163, :241, Research.tsx:123, Skills.tsx:361). No further CC-07-shape misses found.

---

## CC-08 (P4): admin auth header validators silent on operator side

### Diagnosis (from A41 §CC-08)
`mariana/api.py:246-267` `_normalize_bearer_auth_header` had four 500 raises with distinct user-facing `detail` strings pre-Phase-F:
1. `"admin endpoint called without authorization header"` — `raw is None / falsy`
2. `"authorization header is empty"` — `raw.strip()` empty
3. `"expected Bearer authorization header"` — wrong scheme
4. `"empty Bearer token"` — empty token after split

Phase F (commit `39b1d17`) collapsed all four to the identical `"Sign-in failed. Try again, or contact support if this keeps happening."` — correct on the user-facing side (security best practice: do not leak which preflight check failed), but the function emits zero `logger.*` calls, so the operator-facing differentiation that previously lived in `detail` was erased without compensation.

### Fix (commit `9d4ed58`)
Kept all four user-facing detail strings identical (security posture preserved). Added one `logger.warning("admin_auth_header_<reason>", reason=<tag>)` per branch **before** each `raise`. Reason codes:
- `missing` — `not raw`
- `empty` — `value.strip()` empty
- `wrong_scheme` — does not start with `bearer ` (case-insensitive)
- `empty_token` — token empty after split

Logger pattern matches the existing `structlog.get_logger(__name__)` usage at `mariana/api.py:105` (sibling examples at lines 1260, 1264, 1394, 1398, 1608).

### Verification
```bash
$ grep -n 'logger.warning("admin_auth_header' mariana/api.py
251:        logger.warning("admin_auth_header_missing", reason="missing")
258:        logger.warning("admin_auth_header_empty", reason="empty")
264:        logger.warning("admin_auth_header_wrong_scheme", reason="wrong_scheme")
271:        logger.warning("admin_auth_header_empty_token", reason="empty_token")
```
All four validator branches have a `logger.warning` immediately before the `raise`.

### Tests
No dedicated test file added — the change is a pure observability augmentation that does not alter any externally observable HTTP contract (status codes and detail strings unchanged). Existing admin-auth tests in the suite continue to pass.

---

## CC-09 (P4): vault contract drift — empty-string values + `_NAME_RE` trailing-newline

### Diagnosis (from A41 §CC-09)
Two contract drifts:

1. **Empty-string value asymmetry.** `validate_vault_env` (the API ingest / WRITE path, `mariana/vault/runtime.py:103-107`) explicitly drops entries whose value is the empty string (`if len(value) == 0: continue`). `fetch_vault_env` (the worker READ path, `mariana/vault/runtime.py:253-268` pre-fix), in contrast, accepted `""` values as long as `isinstance(v, str)`. Net effect: a corrupted or poisoned blob like `b'{"FOO": ""}'` round-tripped to `{"FOO": ""}` in the agent process, even though that shape is unreachable through the normal ingest API. The agent loop would then install a present-but-empty `FOO` — observably distinct from "FOO missing" for shells / redactors that distinguish `[ -z "$FOO" ]` from `[ -v FOO ]`. Under `requires_vault=True`, the task proceeded without raising, despite the validate path never producing this shape on ingest.

2. **`_NAME_RE` trailing-newline escape.** `_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")` — Python's `$` matches before a trailing `\n`, so `_NAME_RE.match("FOO\n")` returned truthy. A poisoned payload like `b'{"FOO\\n": "v"}'` therefore passed shape validation and could reach the env / log layer, where the trailing newline is a minor log-corruption / spoofing vector. The same regex appeared in both `mariana/vault/runtime.py:116` and `mariana/vault/store.py:136`.

### Fix (commits `65da7b5` + `ca8d6bf`)
1. **`mariana/vault/runtime.py` `fetch_vault_env`:** added `if len(v) == 0:` branch inside the kv-shape loop after the `_NAME_RE` / type guards.
   - Under `requires_vault=True`: `raise VaultUnavailableError(f"vault_env empty_value for task {task_id}: key {k!r}")`. Reason code `empty_value` is embedded in the message so ops can alert distinctly.
   - Under `requires_vault=False`: `logger.warning("vault_env_corrupt_payload_degraded", extra={"task_id": task_id, "reason": "empty_value", "key": k})` and `continue` — the legitimate "no vaulted secrets" degrade-to-`{}` semantics are preserved; the offending key is dropped (matching `validate_vault_env`'s WRITE-path drop).
2. **Regex anchor:** both `_NAME_RE` instances switched from `^[A-Z][A-Z0-9_]{0,63}$` to `^[A-Z][A-Z0-9_]{0,63}\Z`. `\Z` anchors strictly to end-of-string regardless of trailing whitespace / newline.
   - `mariana/vault/runtime.py:119`
   - `mariana/vault/store.py:138`
3. **No test relies on the old behaviour.** `grep -rn "FOO\\\\n\|trailing.*newline" tests/` returned zero existing test references to the old `$` semantics.

### Tests (10 added in `tests/test_cc09_vault_contract_drift.py`)

1. `test_empty_value_with_requires_vault_raises` — `b'{"OPENAI_API_KEY": ""}'` + `requires_vault=True` → `VaultUnavailableError` containing `"empty_value"` AND the offending key name.
2. `test_empty_value_among_valid_entries_with_requires_vault_raises` — mixed valid+empty payload still trips fail-closed.
3. `test_empty_value_without_requires_vault_drops_key_and_warns` — `requires_vault=False` returns dict WITHOUT the empty-value key, plus a `vault_env_corrupt_payload_degraded` warning.
4. `test_only_empty_value_without_requires_vault_returns_empty_dict` — all-empty-values payload degrades to `{}`.
5. `test_name_re_rejects_trailing_newline_runtime` — `vault_runtime._NAME_RE.match("FOO\n") is None`.
6. `test_name_re_rejects_trailing_newline_store` — same for `vault_store._NAME_RE`.
7. `test_name_re_pattern_uses_z_anchor_runtime` — pattern source `endswith(r"\Z")` and NOT `"$"`.
8. `test_name_re_pattern_uses_z_anchor_store` — same for store.
9. `test_trailing_newline_key_with_requires_vault_raises` — end-to-end: `b'{"FOO\\n": "value-string"}'` + `requires_vault=True` → `VaultUnavailableError` with `invalid_kv_shape`.
10. `test_trailing_newline_key_without_requires_vault_dropped` — `requires_vault=False` drops the poisoned key, keeps the valid one, emits warning.

All 10 pass on `ca8d6bf`.

---

## Test counts

| Stage | Count |
|---|---|
| Pre-CC-06 baseline (per A41 / CC-05 fix report) | 472 passed |
| Post-CC-06 (per CC-06 fix report) | 480 passed |
| Post-CC-07/08/09 (this fix) | **490 passed**, 13 skipped, 0 failed |

Net delta from this fix: **+10 tests** (the CC-09 file). CC-07 and CC-08 are observability / copy changes that do not need a dedicated test file (CC-07 is a one-character text edit; CC-08 keeps the externally observable HTTP contract identical).

```
$ python -m pytest --tb=short -q
490 passed, 13 skipped, 2 warnings in 7.70s
```

---

## Files touched

| File | Reason | Commit |
|---|---|---|
| `frontend/src/pages/Product.tsx` | CC-07 copy edit | `c4f1950` |
| `mariana/api.py` | CC-08 logger.warning per branch | `9d4ed58` |
| `mariana/vault/runtime.py` | CC-09 `_NAME_RE` `\Z` + empty-value branch in `fetch_vault_env` | `65da7b5` (rode the parallel CC-06 commit's snapshot) |
| `mariana/vault/store.py` | CC-09 `_NAME_RE` `\Z` | `ca8d6bf` |
| `tests/test_cc09_vault_contract_drift.py` | 10 regression tests | `ca8d6bf` |
| `loop6_audit/REGISTRY.md` | CC-07/08/09 FIXED rows | (uncommitted, parallel agent territory) |
| `loop6_audit/CC07_CC09_FIX_REPORT.md` | this report | (uncommitted) |

---

## Constraints honoured
- Test count went **up** (472 → 490), 0 failures.
- `git pull --rebase` before push (no `--force`).
- CC-06 (parallel fix) untouched; their commit `65da7b5` happened to bundle my `runtime.py` CC-09 changes because we shared a working tree at the moment of their snapshot — I accepted that overlap rather than try to surgically un-bundle, because the bundled hunks are the exact CC-09 change (verified by `git show 65da7b5 -- mariana/vault/runtime.py`) and no behavioural delta was lost.
- No `--force`, no test deletions, no skips added.
