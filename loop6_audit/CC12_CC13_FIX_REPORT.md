# CC-12 + CC-13 fix report

**Date:** 2026-04-28
**Branch:** loop6/zero-bug
**Source audit:** `loop6_audit/A44_post_cc11_reaudit_round2.md` (findings F-A44-01 = CC-12, F-A44-02 = CC-13)

---

## CC-12 — pin every GitHub Action to a 40-char commit SHA

### Threat model

The pre-fix workflow files used floating major-version tags (e.g. `appleboy/ssh-action@v1`,
`actions/checkout@v4`). A floating tag resolves to whatever commit the upstream maintainer
last pointed it at; an attacker who compromises the upstream maintainer's account, takes
over the repo, gets a malicious PR merged, or force-moves the tag silently injects code
into our pipeline at the next workflow invocation. For `appleboy/ssh-action`, that
pipeline has the production SSH private key (`HETZNER_SSH_KEY`), production user
(`HETZNER_USER`), and host (`HETZNER_HOST`) materialised as inputs — full
production-code-execution-as-deploy.

GitHub-owned `actions/*` (checkout / setup-python / setup-node) is conventionally treated
as low-risk because GitHub itself owns the namespace, but the project's 0-bug /
hostile-environment posture says ALL Marketplace actions get pinned to SHA, first-party
or not.

### SHA verification methodology

Every SHA was looked up against the live GitHub API and verified two ways:

1. `GET repos/{owner}/{repo}/git/refs/tags/{tag}` → returns `object.sha` and `object.type`.
   For lightweight tags (the four we pinned) `object.type` is `commit` and `object.sha` is
   the commit SHA directly. For annotated tags, dereference via `git/tags/{tag-sha}`
   (none of our four were annotated).
2. `GET repos/{owner}/{repo}/commits/{sha}` to confirm the commit exists and inspect the
   subject. Each subject was sanity-consistent with the release notes.

**No SHA was invented or hallucinated; every pinned SHA round-trips through the live
GitHub API to its tag.**

### Pinning table

| Action | Old ref | New ref (40-char SHA) | Tag at SHA | Occurrences |
|---|---|---|---|---|
| `actions/checkout` | `@v4` | `@34e114876b0b11c390a56381ad16ebd13914f8d5` | `v4.3.1` | 9 (8 in ci.yml — every job's checkout step including the `fetch-depth: 0` security-checks one — plus 1 in deploy.yml) |
| `actions/setup-python` | `@v5` | `@a26af69be951a213d495a4c3e4e4022e16d87065` | `v5.6.0` | 1 (ci.yml backend-tests) |
| `actions/setup-node` | `@v4` | `@49933ea5288caeca8642d1e84afbd3f7d6820020` | `v4.4.0` | 5 (ci.yml frontend-tests / frontend-lint / frontend-typecheck / frontend-build / security-checks) |
| `appleboy/ssh-action` | `@v1` | `@0ff4204d59e8e51228ff73bce53f80d53301dee2` | `v1.2.5` | 1 (deploy.yml deploy) |

**Total `uses:` references SHA-pinned: 16** — 14 in ci.yml + 2 in deploy.yml; verified via `grep -cE "^\s*uses:" .github/workflows/*.yml` (14 / 2).

### Exact line-by-line list

```
.github/workflows/ci.yml:80   uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
.github/workflows/ci.yml:83   uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0
.github/workflows/ci.yml:123  uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
.github/workflows/ci.yml:126  uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020  # v4.4.0
.github/workflows/ci.yml:156  uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
.github/workflows/ci.yml:159  uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020  # v4.4.0
.github/workflows/ci.yml:187  uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
.github/workflows/ci.yml:190  uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020  # v4.4.0
.github/workflows/ci.yml:216  uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
.github/workflows/ci.yml:219  uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020  # v4.4.0
.github/workflows/ci.yml:242  uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
.github/workflows/ci.yml:259  uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
.github/workflows/ci.yml:268  uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020  # v4.4.0
.github/workflows/ci.yml:289  uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
.github/workflows/deploy.yml:22  uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
.github/workflows/deploy.yml:30  uses: appleboy/ssh-action@0ff4204d59e8e51228ff73bce53f80d53301dee2  # v1.2.5
```

Floating-ref scan after fix:

```
$ grep -nE "uses: [^@]+@(v[0-9]|main|master)" .github/workflows/*.yml
(no output — clean)
```

### Why kept current major versions rather than upgrading

The latest stable releases at fix time were `actions/checkout@v6.0.2`,
`actions/setup-python@v6.2.0`, `actions/setup-node@v6.4.0`, all newer majors than what
the repo currently uses. For 0-bug-tolerance under a tight blast radius we deliberately
held to the latest **patch** of the **currently in-use major** (v4 / v5 / v4) rather
than punting major upgrades through this fix. CC-12 is purely about supply-chain
hardening; functional upgrades to v6 should land separately with their own dependency
testing.

### `appleboy/ssh-action` inline replacement decision

The original audit suggested optionally replacing `appleboy/ssh-action` with an inline
`ssh -o StrictHostKeyChecking=accept-new ...` so the deploy script is auditable in-tree
with no third-party action at all. We considered it and rejected it for this round:

- The action handles `known_hosts` provisioning, multi-line script chunking, exit-code
  propagation, and timeout management — non-trivial to replicate correctly in a single
  CI commit without risk of bricking deploys.
- SHA-pinning to `v1.2.5` already closes the supply-chain attack-surface flagged by the
  audit.
- Tracking the inline-rewrite as a separate, deliberate hardening commit (not bundled
  with this batch) is the safer path.

Defer-decision logged here so a future re-audit knows we chose not to swap out the action.

---

## CC-13 — minimum-permission top-level `permissions:` block

### Threat model

Without a top-level `permissions:` block, every job inherits the repo / org default
`GITHUB_TOKEN` scope. For repos created before 2023-02 (or with the org default still
set to "permissive"), that default is `contents: write, pull-requests: write, …`. A
hypothetical compromise via CC-12's floating action tag could push commits, open PRs,
or modify branch-protection rules via the inherited `GITHUB_TOKEN` — even after
SHA-pinning, defence-in-depth says we squeeze the token's scope to the minimum.

Both `ci.yml` and `deploy.yml` only need `contents: read`.

### Permissions blocks added

#### `.github/workflows/ci.yml`

Top-level (immediately after the `on:` trigger, before `concurrency:`):

```yaml
permissions:
  contents: read
```

#### `.github/workflows/deploy.yml`

Top-level (immediately after the `on:` trigger, before `jobs:`):

```yaml
permissions:
  contents: read
```

### Per-job overrides

**None required.** Each existing job:

- ci.yml `backend-tests` — runs pytest against postgres/redis service containers; no
  GitHub API calls, no commit/PR writes. `contents: read` covers checkout. ✅
- ci.yml `frontend-tests` / `frontend-lint` / `frontend-typecheck` / `frontend-build` —
  npm-based, no GitHub API calls. ✅
- ci.yml `sql-lint` — bash + git-grep against the checked-out tree. ✅
- ci.yml `security-checks` — secret-scan + npm audit; the `fetch-depth: 0` checkout
  needs full history but that's a checkout argument, not a permissions argument. ✅
- ci.yml `registry-integrity` — bash + grep against the checked-out tree. ✅
- deploy.yml `deploy` — checks out the repo, then hands off to remote `/opt/mariana` over
  SSH. Reads the repo, never writes back to GitHub. ✅

If a future job is added that needs `contents: write`, `pull-requests: write`, or
`packages: write` — for example a release-tagger, an auto-merge bot, or a GHCR push — it
**must** declare those at the JOB LEVEL with the minimum necessary scope, never widen
the top-level block. The top-level grant remains `contents: read` so the inherited
default for unscoped jobs stays minimal.

---

## Verification

### YAML parse

```
$ python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); print('ci.yml: OK')"
ci.yml: OK
$ python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml')); print('deploy.yml: OK')"
deploy.yml: OK
```

### actionlint

`actionlint` not installed in the local sandbox; relying on YAML-parse + GitHub's
built-in validation at workflow run time. The structural changes (SHA pins + top-level
`permissions:` block) are well-defined GitHub-Actions-spec features, not novel syntax.

### Floating-ref grep

```
$ grep -nE "uses: [^@]+@(v[0-9]|main|master)" .github/workflows/*.yml
(no output — all pinned to 40-char SHAs)
```

### pytest

```
$ PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb python -m pytest -q
… 509 passed, 13 skipped, 2 warnings in 7.76s
```

Baseline 509+/13/0 maintained. ✅

### vitest

```
$ cd frontend && npm test -- --run
… Test Files  15 passed (15)
       Tests  144 passed (144)
```

Baseline 144 maintained. ✅

### registry-integrity

```
$ bash .github/scripts/check_registry_integrity.sh
REGISTRY.md: zero OPEN findings — convergence invariant holds.
```

CC-12 / CC-13 rows added to REGISTRY.md as **FIXED 2026-04-28**, no new OPEN rows. ✅

---

## Commits

- `CC-12 pin all GitHub Actions to commit SHAs (supply-chain hardening)`
- `CC-13 add minimum-permission top-level permissions blocks to ci and deploy workflows`

Both signed off as `fpkgvip <fpkgvip@gmail.com>` to match the existing commit-author
identity for this work stream. No `--force` push.
