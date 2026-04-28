# A46 — Post-CC-14 re-audit

**Repo:** `mariana`
**Branch:** `loop6/zero-bug`
**HEAD audited:** `f5f9574`
**Primary commit:** `f5f9574` (CC-14: narrow secret-scan exclusion to scanner file only)
**Cumulative range spot-check:** `c108b1e..f5f9574` (CC-04..14 + Phase D + Phase F batches 1+2)
**Auditor:** opus (adversarial; round 1/3 of post-CC-14 streak)
**Pytest baseline:** 509 passed / 13 skipped / 0 failed (per mandate)
**Vitest baseline:** 144 passed (per mandate)

## Verdict

One new finding: the `deploy.yml` workflow lacks a `concurrency` block, so two pushes to `master` (or a push that races a `workflow_dispatch`) can run two deploys against the same Hetzner host in parallel and stomp each other mid-`docker compose build/up`.

CC-14 itself is correctly applied — the secret-scan exclusion is now narrowed to the scanner file alone, the rest of `.github/scripts/` is scanned, and the gate still passes on the current tree (no committed token in any helper script trips the patterns). All four CI focus areas (PR triggers, secret-print paths, action SHA pins, top-level permissions) remain clean.

## Findings

### F-A46-01 — `deploy.yml` has no `concurrency` block, so concurrent pushes to `master` can race two production deploys against the same host — **P4**

- **File:line:** `.github/workflows/deploy.yml:1-18` (no `concurrency:` key anywhere in the file).
- **Trigger:**
  ```yaml
  on:
    push:
      branches: [master]
    workflow_dispatch:
  ```
- **Class:** CI / deploy hardening — race-condition risk in production deploy path.
- **Description:** `ci.yml` correctly declares `concurrency: {group: ci-${{ github.ref }}, cancel-in-progress: true}`, but `deploy.yml` has no concurrency control at all. The deploy job SSHes into the production Hetzner host and runs `git fetch origin master && git reset --hard origin/master && docker compose build --no-cache mariana-api mariana-orchestrator && docker compose up -d --force-recreate ...`. Two concurrent runs (e.g. a fast follow-up push to `master`, or a push that races a manual `workflow_dispatch` trigger) will both SSH in, both `git reset --hard`, and both `docker compose build/up` against the same compose project at the same time. That can:
  1. Leave the working tree at an indeterminate commit (whichever `git reset --hard` lands second wins, but the first run's `docker build` may have already used the wrong source tree).
  2. Have the two `docker compose build` invocations interleave Buildx cache writes — Buildx is generally safe under concurrent builds for the same target, but `docker compose up -d --force-recreate` is not safe when two callers are simultaneously recreating the same container; whichever finishes second can leave the service in an unintended generation, while the health-check loop in the first run reports success against the second run's container.
  3. Defeat the deploy's own health gate: the `for i in $(seq 1 12); do curl -sf http://localhost:8080/api/health ...` loop assumes it is the only writer; under racing deploys, an `exit 0` from one run can mask a still-broken state established by the other.
- **Compounding factor:** `workflow_dispatch` is an explicit override mechanism. An operator running `workflow_dispatch` precisely because something is broken in production is the most likely path to triggering this race (push lands at the same moment, or operator triggers two manual runs in close succession to "hurry it up").
- **Reproduction / discovery:** Read `.github/workflows/deploy.yml` end-to-end and search for `concurrency:` — there is none. Compare to `ci.yml:26-28` which has it. The deploy script itself does not implement any host-side mutex (no `flock`, no lock file under `/opt/mariana`).
- **Why this is P4:** This is a real ops correctness issue, but it requires two pushes (or push + manual dispatch) within the deploy window (~1–2 minutes) to actually fire. It does not directly leak secrets or expose a vulnerability — it can only corrupt the deployed state.
- **Fix sketch (discovery only — do not apply):** Add a top-level `concurrency: {group: deploy-${{ github.ref }}, cancel-in-progress: false}` block to `deploy.yml` so the second deploy waits for the first to finish (rather than cancelling, which could abort mid-build). Optionally also add a host-side `flock /var/lock/mariana-deploy.lock` wrapper around the SSH script body for defence in depth.

## Audit notes by mandate focus area

### 1) CC-14 fix verification

The fix is correctly applied. `.github/scripts/check_secrets.sh` now reads:

```bash
EXCLUDES=(
  ':!loop6_audit/'
  ':!.github/scripts/check_secrets.sh'   # was ':!.github/scripts/' before CC-14
  ':!tests/'
  ':!frontend/package-lock.json'
  ':!frontend/node_modules/'
  ':!frontend/src/test/'
)
```

Running the script on the current tree:

```
$ bash .github/scripts/check_secrets.sh
Secret scan: no high-confidence tokens found in tracked files.
```

(exit 0)

I positive-controlled the patterns by writing a synthetic `sk_live_...` token into a throwaway repo and confirmed the same regex flags it, so the patterns themselves are not silently broken.

I then re-`git grep`ed every pattern across the unexcluded `.github/scripts/` files (`apply_migrations.sh`, `check_migration_pairs.sh`, `check_registry_integrity.sh`, `ci_full_baseline.sql`, `ci_pg_bootstrap.sql`) and found zero matches for any of the six patterns, so the new in-scope tree contains nothing that would falsely trip the gate.

### 2) Other CI workflow gaps

- **Concurrency:** see F-A46-01 above (`deploy.yml` lacks a concurrency block; `ci.yml` has one).
- **Timeouts:** every job in both workflows declares `timeout-minutes` (5–20). No untimed jobs.
- **PR triggers:** no workflow uses `pull_request_target`. `ci.yml` uses ordinary `pull_request` plus `push`. `deploy.yml` uses only `push` to `master` plus `workflow_dispatch`.
- **Secret print:** no `run:` step echoes `${{ secrets.* }}`. The deploy step passes Hetzner secrets as action inputs to the pinned SSH action (expected).
- **Scheduled jobs:** there are no `schedule:` triggers anywhere, so "scheduled jobs missing concurrency limits" is not applicable.
- **Action pins:** all four third-party uses (`actions/checkout`, `actions/setup-python`, `actions/setup-node`, `appleboy/ssh-action`) are still pinned to 40-char SHAs, matching A45's verification.
- **Top-level permissions:** both workflows still declare `permissions: {contents: read}` and no job needs more.

### 3) Forbidden-words paranoid pass

I re-grepped (case-insensitive) the full forbidden-words list — `supercharge|empower|unlock|transform|accelerate|revolutionize|reimagine|magical|amazing|stunning|seamless|effortless|world-class|next-gen|cutting-edge|scrape|scraping|crawl|crawling` — across the four mandated surfaces:

- **`frontend/src/`** — every hit is benign:
  - `transform` / `transition-transform` are CSS class names from Tailwind / d3 / Radix UI primitives.
  - `Unlock` / `unlock` are legitimate vault UX strings ("Unlock vault", `mode === "unlock"`).
  - `b26_fileviewer_markdown_xss.test.ts` mentions "transform" inside a comment describing the markdown link-transform rule.
  - No marketing-tone violations.
- **`frontend/public/`** — no matches.
- **`mariana/api.py`** — two hits, both inside LLM prompt strings, neither in a `HTTPException(detail=...)` nor in any `BaseModel` field/description:
  - line 2062: `"scrape a page"` is a quoted example user request inside RULE 2a of the chat classifier prompt.
  - line 8686: `"log scrape"` is part of a docstring describing a defence-in-depth check on an admin endpoint.
- **`mariana/main.py`** — no matches.
- **`.github/workflows/*.yml`** — no matches in job names, descriptions, or comments.

I also explicitly grepped `HTTPException(detail=` lines and `class *Response` / `class *Model` lines in `api.py` for the marketing tokens — zero hits. So no surface-level copy regression.

### 4) Frontend XSS / injection risk

- **`dangerouslySetInnerHTML`** appears in four places. All four are safe today:
  1. `frontend/src/components/FileViewer.tsx:339` via `renderMarkdownContent` — escapes `& < > " '` first, isolates fenced code blocks behind null-byte tokens, and explicitly rejects any non-`https?://` href in the `[text](url)` rule (B-26 fix). I re-read the function end-to-end; the link-transform regex re-decodes the URL after entity-escape and re-validates the scheme before re-escaping for attribute injection.
  2. `frontend/src/pages/Chat.tsx:3455` via `renderMarkdown` — escapes `& < >`, isolates `<pre>` blocks, and only emits `<a href>` for `https?://[^)]{1,500}` URLs (the regex itself excludes any `javascript:` or `data:` scheme by construction). Quotes/backticks inside link URL or text are HTML-entity-encoded to prevent attribute breakout.
  3. `frontend/src/components/ui/chart.tsx:70` — the inner string is built from app-controlled `THEMES`, `chart-${id}` (where `id` is `React.useId()` or a developer-supplied prop), and `itemConfig.color` from the developer-supplied `ChartConfig`. No runtime user input lands in that string today.
  4. `Chat.tsx`'s `extractCitations` regex at line 397 (`/\[([^\]]{1,200})\]\((https?:\/\/[^)]{1,500})\)/g`) — only `https?://` URLs survive into `<a href={c.url}>` in the rendered citation chips.

- **`<a href={url}>` with non-validated `url`:** the only places where `url` originates from agent-controlled data and is passed directly to `href` without an explicit scheme guard are:
  - `frontend/src/components/deft/LiveCanvas.tsx:320` — renders artifact links with `href={url}` where `url = (a.url as string) ?? (a.signed_url as string)` and `a` is an agent artifact dict.
  - **However**, the server-side `mariana/agent/models.py:109 AgentArtifact` declares `model_config = ConfigDict(extra="forbid")` and contains only `name | workspace_path | size | sha256 | produced_by_step` — no `url` and no `signed_url`. Any artifact dict with a `url` field would fail Pydantic validation in `_load_agent_task`, so the LiveCanvas access path is dead today: `(a.url as string)` is always `undefined`, the conditional `{url && ...}` hides the link, and there is no exploit path on the current tree.
  - This is therefore a **dormant defence-in-depth gap**, not an active finding: a future commit that adds a `url` field to the artifact schema (e.g. for signed download URLs) would silently introduce a `javascript:`-href XSS unless the same commit also adds a client-side scheme guard. `LiveStudio.tsx`'s sibling `deriveLiveUrl` already enforces `^https?:|^/preview/` on the same payload shape, which suggests the team is aware. Worth filing as a small follow-up but it is not a current bug, and the file was not touched in `c108b1e..f5f9574` so it is also not a regression of the audited range.

- **`<a href={url}>` elsewhere:** `AppErrorBoundary` and `DevObservability` use `buildReportIssueUrl()` which always constructs `mailto:`. `Footer.tsx` uses `mailto:${BRAND.supportEmail}` from a constant. `PreviewPane.tsx` uses `previewAbsoluteUrl(rel_url)` which prepends `apiBase` to any non-`https?://` value, so the worst case is `apiBase//evil.com/x` — and the upstream `rel_url` is constructed server-side as `"/preview/{task_id}/{entry}"`, which always produces a `/preview/...` same-origin path even if `entry` is hostile. `LiveStudio` enforces `^https?:|^/preview/` via `deriveLiveUrl`. All other matches are static strings or `useId`-derived.

- **`innerHTML` / `outerHTML` / `document.write` / `insertAdjacentHTML`:** zero hits in `frontend/src/`. The single `setAttribute("href", href)` in `frontend/src/lib/pageHead.ts` writes a canonical link tag whose `href` is computed from app config — no runtime user input.

- **`eval` / `new Function`:** one hit, `frontend/src/lib/observability.ts:69` `new Function("m", "return import(m)")` — that is the standard pattern for synthesising a dynamic `import()` so a static analyser doesn't bundle it. The argument `m` is the module path `m`, controlled by the call site (not user input), and the function body is a hard-coded string. Not exploitable.

### 5) SSRF

`mariana/connectors/sec_edgar_connector.py:236` (CC-10 fix) is correctly anchored:

```python
if not _re.match(r"^([a-z0-9-]+\.)*sec\.gov\Z", parsed_host):
    raise ConnectorError(f"Filing URL must be on *.sec.gov, got: {parsed_host}")
```

The `\Z` anchor blocks the previous trailing-newline bypass. I positive-controlled this in my head: `parsed_host = "evil.com\nsec.gov"` would not match `\Z` (which only matches end-of-string), and `urlparse("https://evil.com\nsec.gov/").hostname` lower-cases to `evil.com\nsec.gov` regardless, so the regex correctly rejects it.

The other connectors (`fred_connector`, `polygon_connector`, `unusual_whales_connector`) all hit fixed base URLs derived from config — no user-controlled URL is fed to `client.request()` from those paths.

The base SSRF defence in `mariana/connectors/base.py` (`_validate_initial_url` plus `_ssrf_redirect_hook`) is unchanged in the audited range. It is functionally correct (rejects RFC1918 / loopback / link-local / multicast / reserved / unspecified, blocks redirects to internal hosts via the response hook). The well-known TOCTOU between the `_validate_initial_url` resolution and httpx's own resolution is a pre-existing defence-in-depth limitation, not introduced or aggravated by the audited range, so I am not raising it as a new finding.

I also checked redirect handling for the Stripe checkout `success_url` / `cancel_url`: `mariana/api.py:5565-5576` validates that `parsed.hostname` is in `_ALLOWED_REDIRECT_HOSTS`. `urlparse("javascript:alert(1)").hostname` is `None` and `urlparse("data:...").hostname` is `None`, neither is in the allowed-set, so both are rejected with a 400. The check correctly defends against scheme-smuggling.

No other route in `mariana/api.py` accepts an arbitrary `url` field in a request model — I grepped for `url:.*str|url: HttpUrl|url=Body|url=Query|url=Form` and only saw the Stripe checkout fields plus internal redact / config helpers.

### 6) Concurrency / RLS

- **`set local row_security`:** zero hits anywhere under `mariana/` or `frontend/supabase/`. The bypass keyword is not used, so there is no trusted-path / untrusted-path split to audit.
- **`SECURITY DEFINER` with unpinned `search_path`:** I parsed every migration under `frontend/supabase/migrations/` and audited each `CREATE [OR REPLACE] FUNCTION ... SECURITY DEFINER` for an explicit `SET search_path` clause.
  - All forward migrations (`001..024`, plus `004b`) pin `search_path` on every SECURITY DEFINER function I checked.
  - Only `frontend/supabase/migrations/007_revert.sql` declares 10 SECURITY DEFINER functions (`add_credits`, `admin_count_profiles`, `admin_list_profiles`, `check_balance`, `deduct_credits`, `get_stripe_customer_id`, `get_user_tokens`, `handle_new_user`, `update_profile_by_id`, `update_profile_by_stripe_customer`) without a pinned `search_path`. That file restores the pre-007 versions so the absence is structurally faithful to history, but it does mean a rollback of migration 007 reintroduces a search-path-injection class problem on those 10 functions.
  - `git log --diff-filter=A` shows `007_revert.sql` was added in commit `3819547` and has not been modified in `c108b1e..f5f9574`. Since the audit range is "all of CC-04..14 + Phase D + Phase F batches 1+2", and this file was not touched, this is a **pre-existing condition**, not a new finding from the audited range. Flagging here for visibility only — it should be tracked separately if the rollback path is ever expected to run on a hostile session search_path.

### 7) Secrets in committed env templates

`git ls-files | grep -E '\.env|env\.'` returns three paths: `.env.example`, `frontend/.env.example`, and `frontend/src/vite-env.d.ts`. `.env` itself is gitignored at `/.env` (line 2 of `.gitignore`).

I read both `.env.example` files end-to-end. Every value is either a placeholder (`your_polygon_api_key_here`, `your-anon-key-here`), an empty string awaiting deploy, or a documentation comment. No `sk_live_`, `pk_live_`, `whsec_`, `AKIA`, `xox[pboa]-`, or `gh[pousr]_` token is present. The secret-scan job catches this case anyway, but the manual review confirms.

### 8) Deltas vs prior audits A40..A45

- A40 (CC-02 phase D), A41 (CC-05), A42 (CC-09), A43 (CC-11), A44 (CC-11 r2), A45 (CC-13) — I re-checked the surfaces each prior audit reviewed and found no regression:
  - The A39 `c108b1e` migrations top-out claim still holds: forward set is `001..022, 004b, 024` (24 forward files), no new `023_*` and nothing newer than `024_*`.
  - The A45 secret-scan-coverage gap (F-A45-01) is now closed by CC-14 — confirmed by reading `check_secrets.sh` and confirming the directory exclusion is gone.
  - All four pinned action SHAs from A45 still match the upstream tags I quoted there (`v4.3.1`, `v5.6.0`, `v4.4.0`, `v1.2.5`).
  - No new `pull_request_target`, no new `permissions: write-all`, no new floating `@v4` action ref.

The only new finding I uncovered that the prior audits did not flag is the missing `concurrency` block on `deploy.yml`, which has been latent since `2776060` (initial deploy workflow) — the recent CC-12 / CC-13 commits touched the file but did not address this specific control.

## Bottom line

- **1 finding (P4)**: `deploy.yml` lacks a `concurrency:` block, so concurrent `master` pushes (or push + `workflow_dispatch`) can race two production deploys. Not introduced by CC-14 itself, but in scope for the cumulative range.
- **CC-14 is correctly landed**: the secret-scan exclusion is now scoped to the scanner file alone, the gate passes on the current tree, and no helper script in `.github/scripts/` contains anything that would falsely trip the patterns.
- **No XSS / injection regression** in the frontend: every `dangerouslySetInnerHTML` and every `<a href={url}>` either escapes/sanitises correctly or is structurally unreachable (LiveCanvas artifact `url` is filtered out by Pydantic's `extra="forbid"` on `AgentArtifact`).
- **No new SSRF**: CC-10 sec_edgar fix holds; other connectors hit fixed base URLs; Stripe `success_url` / `cancel_url` is hostname-allowlisted.
- **No new RLS / SECURITY-DEFINER gap**: no `row_security` bypass anywhere; every forward-migration SECURITY DEFINER function pins `search_path`. The 10 unpinned functions in `007_revert.sql` are pre-existing and out-of-scope for the audited range.
- **No secrets in tracked env templates**.
- **No forbidden-word copy regression** on `frontend/src/`, `frontend/public/`, `mariana/api.py` (HTTPException details / response models), `mariana/main.py` (startup messages), or `.github/workflows/*.yml` (job names / descriptions).

## Confidence assessment

- **High confidence** that CC-14 itself is correctly implemented and the secret-scan gate passes on the current tree (verified by direct invocation, by reading the diff, and by positive-control on a synthetic token).
- **High confidence** that no new XSS / SSRF / injection vulnerability landed in the cumulative range (all four `dangerouslySetInnerHTML` sites and every variable `<a href>` site re-read end-to-end; agent artifact `url` path traced to a Pydantic schema with `extra="forbid"`).
- **Medium-high confidence** on the deploy concurrency finding — the missing `concurrency` block is unambiguous in the YAML, but the actual blast radius depends on operational realities (push frequency, whether anyone uses `workflow_dispatch`, whether docker compose recreate is idempotent under that specific workload).
- **Lower confidence** that I have not missed a non-obvious supply-chain or PostgREST / RLS issue in the broader cumulative range — I did not re-execute pytest or vitest, and I did not re-walk every line of every migration. The mandate framed CC-14 plus a paranoid sweep, and this report reflects that scope.
