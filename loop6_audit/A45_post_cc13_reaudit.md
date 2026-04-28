# A45 — Post-CC-13 re-audit

**Repo:** `mariana`
**Branch:** `loop6/zero-bug`
**HEAD audited:** `ab2fd28`
**Primary range:** `465c5ee..ab2fd28` (CI hardening commits)
**Cumulative range spot-check:** `c108b1e..ab2fd28`
**Auditor:** gpt (adversarial)

## Verdict

One new finding: the new CI secret-scan control has a blind spot that skips the entire `.github/scripts/` tree, so secrets committed into CI helper scripts or SQL baselines would not fail the security gate.

## Findings

### F-A45-01 — `check_secrets.sh` excludes the entire `.github/scripts/` directory, leaving CI helper scripts unscanned for committed secrets — **P4**

- **File:line:** `.github/scripts/check_secrets.sh:25-35`
- **Code:**
  ```bash
  EXCLUDES=(
    ':!loop6_audit/'
    ':!.github/scripts/'
    ':!tests/'
    ':!frontend/package-lock.json'
    ':!frontend/node_modules/'
    ':!frontend/src/test/'
  )
  ```
- **Class:** CI hardening / secret-detection coverage gap.
- **Description:** The new `security-checks` job relies on `.github/scripts/check_secrets.sh` to block obvious committed tokens, but that script excludes the whole `.github/scripts/` directory rather than only excluding itself. This means any accidental or malicious secret committed into CI helper shell scripts, bootstrap SQL, or future support files under `.github/scripts/` will be invisible to the gate. That is exactly the directory where this range introduced multiple new operational files (`apply_migrations.sh`, `ci_full_baseline.sql`, `ci_pg_bootstrap.sql`, etc.), so the control currently leaves part of the newly added CI surface unprotected.
- **Reproduction / discovery:** A positive-control `git grep` finds text inside `.github/scripts/apply_migrations.sh`, but the same search with the exact pathspec exclusions used by `check_secrets.sh` omits that file entirely, proving the whole directory is skipped rather than just the scanner itself.
- **Why this is P4:** This is a real security-control failure, but it requires a separate mistaken or malicious secret commit into the excluded tree to become an exposure. It weakens defense in depth rather than directly exposing a live secret on its own.
- **Fix sketch (discovery only — do not apply):** Narrow the exclusion from `:!.github/scripts/` to the scanner file itself (for example `:!.github/scripts/check_secrets.sh`), or keep the directory scanned and suppress only the exact self-referential regex lines.

## Audit notes by mandate

### 1) CC-12 SHA pinning

I checked the full workflow YAMLs and found no remaining `@vN` action references anywhere under `.github/`.

The pinned SHAs now match the upstream tag refs for the intended versions:

- `actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5` ↔ `v4.3.1`
- `actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065` ↔ `v5.6.0`
- `actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020` ↔ `v4.4.0`
- `appleboy/ssh-action@0ff4204d59e8e51228ff73bce53f80d53301dee2` ↔ `v1.2.5`

So CC-12 itself looks correctly landed.

### 2) CC-13 permissions

Both workflow roots now declare:

```yaml
permissions:
  contents: read
```

I read both full workflow files. I did not find any job that obviously needs broader GitHub token scopes:

- `ci.yml` jobs only check out code, install dependencies, run tests/lint/build/scripts, and use service containers.
- `deploy.yml` checks out code and then hands off to the remote host over SSH.
- No job posts PR comments, writes releases, uploads attestations, or mutates repository state.

So CC-13 also looks correctly landed.

### 3) PR-trigger workflow security holes

No workflow uses `pull_request_target`.

`ci.yml` is on ordinary `pull_request` plus `push`, and `deploy.yml` is only `push` to `master` plus `workflow_dispatch`.

### 4) Secrets exposure in logs

I did not find any workflow step that echoes or prints `${{ secrets.* }}` into a shell logging context.

The deploy workflow passes Hetzner secrets as action inputs to the pinned SSH action, which is expected; I found no direct `echo`, `printf`, or inline `run:` use of secret expressions.

### 5) Full diff re-grep (`c108b1e..ab2fd28`)

I re-read the cumulative diff and spot-checked the workflow/script/frontend/migration changes. The only new bug I found in that pass was the secret-scan exclusion gap above.

I did not find a surviving floating action tag, a missing top-level permissions block, a `pull_request_target` trigger, or a workflow secret-print path.

### 6) Migrations directory

The migrations directory still tops out at `024_bb01_refund_credits_aggregate_ledger.sql` plus `024_revert.sql`.

There is still no `023_*` forward migration, but there is also no file newer than `024_*`.

Forward migrations remain 24 total: `001` through `022`, plus `004b`, plus `024`.

### 7) Frontend forbidden words paranoid pass

I re-grepped `frontend/src/` and `frontend/public/` for the forbidden words list (`build`, `ship`, `supercharge`, `empower`, `unlock`, `transform`, `accelerate`, `revolutionize`, `reimagine`, `magical`, `amazing`, `stunning`, `seamless`, `effortless`, `beautiful`, `powerful`, `smart`, `next-gen`, `cutting-edge`, `world-class`, `scrape`, `scraping`, `crawl`).

This pass returned no matches in the scanned frontend source/public content, so I did not find a new user-facing copy regression on that surface.

## Bottom line

- **1 finding (P4)**: the new secret-scan gate skips the entire `.github/scripts/` tree.
- **CC-12 looks correct**: all workflow action refs are pinned to full SHAs, and the spot-checked SHAs resolve to the intended upstream tags.
- **CC-13 looks correct**: both workflows have top-level `permissions: contents: read`, and I did not find a job that needs more.
- I found no remaining floating action tags, no `pull_request_target`, no direct secret logging, no new migrations beyond `024`, and no forbidden-word frontend copy regressions in the paranoid pass.
