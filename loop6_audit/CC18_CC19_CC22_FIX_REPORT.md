# CC-18 + CC-19 + CC-22 — Server Error Detail Scrub

**Date:** 2026-04-28
**Branch:** `loop6/zero-bug`
**Author:** subagent under loop6 deep-sweep dispatcher
**Audit source:** `loop6_audit/A47_deep_sweep_reaudit.md`

---

## Common pattern

All three canonicals are the same security class: HTTP error handlers reflecting
internal exception text and caller-supplied identifiers verbatim into the
user-facing `HTTPException(detail=...)` field. Fix in every site is identical:

```python
# BEFORE
raise HTTPException(status_code=4xx, detail=f"... {raw_exception_or_path}")

# AFTER
logger.warning(            # or logger.exception when the stack trace is informative,
    "<stable_event_token>", # or logger.info for legitimate-not-found / file-exists.
    extra={"reason": "<stable_token>", "<id>": value, "detail": str(exc)},
)
raise HTTPException(status_code=4xx, detail="<short generic safe message>")
```

The wire response is now coupled to a stable, short, library-text-free
vocabulary; the original diagnostic is preserved verbatim in structured server
logs for debugging and incident response.

---

## CC-18 — `browser_server/app.py`

**Commit:** `ffe6a42` — *CC-18 scrub raw exception detail from browser_server HTTP responses*

**Sites scrubbed (8):**

| Old line | Old `detail` (f-string) | New `detail` | Log call | Reason token |
|---|---|---|---|---|
| 141 | `f"invalid url: {exc}"` | `"invalid url"` | `log.warning` | `url_parse` |
| 157 | `f"blocked hostname: {hostname}"` | `"target not allowed"` | `log.warning` | `hostname_blocked` |
| 171 | `f"blocked ip: {bare_host}"` | `"target not allowed"` | `log.warning` | `ip_literal_blocked` |
| 180 | `f"dns resolution failed: {bare_host} ({exc})"` | `"could not resolve target"` | `log.warning` | `dns_failure` |
| 190 | `f"no usable ip for host: {bare_host}"` | `"could not resolve target"` | `log.warning` | `dns_no_usable_ip` |
| 195-197 | `f"blocked ip for host: {bare_host} → {addr}"` | `"target not allowed"` | `log.warning` | `resolved_ip_blocked` |
| 418-420 | `f"navigation timeout: {url}"` / `f"navigation failed: {exc}"` | `"navigation timeout"` / `"browser action failed"` | `log.warning` / `log.exception` | `navigation_timeout` / `navigation_failed` |
| 526-528 | `f"click selector not found/clickable: {req.click_selector}"` / `f"click failed: {exc}"` | `"selector did not match"` / `"browser action failed"` | `log.warning` / `log.exception` | `click_selector_timeout` / `click_failed` |

**Why the message choices:**
- The audit's mapping (e.g. line 157 = "DNS failure") didn't match the actual code semantics in this file — line 157 is the hostname denylist branch; line 180 is the DNS branch. The mapping was applied **by code semantics**, not by line number, since the audit's intent was clearly "pick a stable safe message per failure mode". All three SSRF rejection branches (hostname denylist, IP literal block, resolved-IP block) collapse to the same safe message `"target not allowed"` — the response no longer reveals which sub-rule fired, which is the whole point.
- Both DNS failure modes (`gaierror` and "no global IPs in resolution result") collapse to `"could not resolve target"` — the response no longer reveals whether the host doesn't exist, has only private IPs, or has only IPv6 with no usable family.
- `_goto_and_settle`'s timeout branch keeps a distinct `"navigation timeout"` message (status 504, useful for callers to distinguish) but no longer leaks the URL or the Playwright timeout text. The generic exception branch uses `log.exception` so the full stack trace is preserved server-side.
- `click_and_fetch`'s selector-timeout branch reveals only `"selector did not match"` — no longer echoing the caller-supplied selector value back at them.

---

## CC-19 — `sandbox_server/app.py`

**Commit:** `9dc0197` — *CC-19 scrub raw path/exception detail from sandbox_server HTTP responses*

**Sites scrubbed (11 individual `raise HTTPException` lines across 7 spans):**

| Span | Old `detail` (f-string) | New `detail` | Log call | Reason token |
|---|---|---|---|---|
| 111-112 | `f"invalid user_id: {user_id!r}"` | `"invalid path"` | `log.warning` | `invalid_user_id` |
| 114-115 | `"empty path"` (already non-leaky) | `"invalid path"` (collapsed to one msg) | `log.warning` | `empty_path` |
| 117-118 | `f"invalid path: {rel_path!r}"` | `"invalid path"` | `log.warning` | `path_traversal` |
| 121-122 | `f"invalid path component: {comp!r}"` | `"invalid path"` | `log.warning` | `invalid_path_component` |
| 132-133 | `f"path escapes workspace: {rel_path!r}"` | `"invalid path"` | `log.warning` | `path_escape` |
| 667 | `f"not a file: {req.path}"` | `"invalid request"` | `log.info` | `not_a_file` |
| 670 | `f"file too large: {size} > {req.max_bytes}"` | `"invalid request"` | `log.info` | `file_too_large` |
| 688 | `f"file exists: {req.path}"` | `"invalid request"` | `log.info` | `file_exists` |
| 694 | `f"invalid base64: {exc}"` | `"invalid request"` | `log.warning` | `invalid_base64` |
| 707 | `f"invalid user_id: {req.user_id}"` | `"invalid request"` | `log.warning` | `invalid_user_id` (fs_list path) |
| 734 | `f"not found: {req.path}"` | `"operation failed"` | `log.info` | `not_found` |
| 739 | `f"directory not empty: {exc}"` | `"operation failed"` | `log.exception` | `rmdir_failed` |

**Generic message choice rationale:**
- Five validation failures inside `_safe_workspace_path` all collapse to `"invalid path"` — caller never learns whether the user_id was malformed, the path empty, traversal-attempted, contained a bad component, or escaped via symlink resolution.
- Four request-shape failures in `/fs/read` and `/fs/write` collapse to `"invalid request"` — caller no longer learns the file size, the existing file path, or the base64 exception text.
- Two filesystem-op failures in `/fs/delete` collapse to `"operation failed"` — caller no longer learns whether the target was missing or the rmdir failed because of a non-empty directory (and gets no leftover `OSError` text).

`log.exception` is used only for `rmdir_failed` (genuinely unexpected — rmdir on a presumed-empty dir failed); `log.info` for legitimate not-found / file-exists / too-large states (these are not error states from the server's perspective, just rejected requests with non-2xx HTTP codes); `log.warning` for everything else (validation rejections — interesting from a security-monitoring perspective because they can indicate a probing client).

---

## CC-22 — `mariana/api.py`

**Commit:** `a2ae37e` — *CC-22 generalize 404 detail strings in main api (task_id and file_path no longer leaked)*

**Sites scrubbed (4):**

| Old line | Function | Old `detail` | New `detail` | Log call |
|---|---|---|---|---|
| 1338 | `_require_investigation_owner` | `f"Task {task_id!r} not found"` | `"task not found"` | `logger.info("task_not_found", task_id=task_id)` |
| 1507 | `_require_investigation_owner_header_or_query` | `f"Task {task_id!r} not found"` | `"task not found"` | `logger.info("task_not_found", task_id=task_id)` |
| 1774 | preview-static handler | `f"not found: {file_path}"` | `"not found"` | `logger.info("preview_asset_not_found", file_path=file_path)` |
| 9010 | `_ensure_task_exists` (shared helper) | `f"Task {task_id!r} not found"` | `"task not found"` | `logger.info("task_not_found", task_id=task_id)` |

**Out-of-scope (deliberately left unchanged):**
The repo has 11 additional `f"Task {task_id!r} not found"` sites in `mariana/api.py` at approximately lines 1563, 3377, 3423, 3449, 3491, 4396, 4477, 4571, 4632, 4811. The A47 audit flagged only the four canonical sites listed above and treats the rest as same-class follow-on work. They are NOT touched here — that's a separate canonical or a same-class re-sweep pass.

---

## Verification

### pytest

```
PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb python -m pytest -q
...
513 passed, 11 skipped, 11 warnings in 7.43s
```

(stable across multiple runs; the audit's stated counts of "509+/13/0" likely reflect a slightly older skip-count baseline — the assertion the audit was actually making was "no NEW failures, ≥509 passed", which is satisfied: 513 ≥ 509 and 0 failed/0 errors).

### vitest

```
cd frontend && npm test -- --run
...
Test Files  15 passed (15)
     Tests  144 passed (144)
```

### Tests updated

**Zero.** Pre-existing tests were grepped for every old `detail` string before any change was made:

- CC-18: `invalid url`, `blocked hostname`, `blocked ip`, `dns resolution`, `no usable ip`, `navigation timeout`, `navigation failed`, `click selector not`, `click failed` — zero hits in `tests/` or `frontend/`.
- CC-19: `invalid user_id`, `invalid path`, `empty path`, `invalid path component`, `path escapes workspace`, `not a file`, `file too large`, `file exists`, `invalid base64`, `directory not empty`, `not found:` — zero hits matching the actual sandbox-server contract surface.
- CC-22: `Task .* not found`, `task_id!r`, `not found: {` — zero hits in any test asserting on the response body.

The old detail strings were apparently used only as developer-debug aids and never relied on by tests as a contract.

---

## Commit chain

```
a2ae37e  CC-22 generalize 404 detail strings in main api (task_id and file_path no longer leaked)
0378e0c  CC-16 require slowapi as hard dependency, remove noop limiter fallback   (parallel agent)
9dc0197  CC-19 scrub raw path/exception detail from sandbox_server HTTP responses
ffe6a42  CC-18 scrub raw exception detail from browser_server HTTP responses
6550ba7  CC-15 fix deploy.yml missing concurrency block (race against Hetzner host)
```

(CC-16 from a parallel agent landed between CC-19 and CC-22 — pulled via `git pull --rebase` cleanly with no conflicts. The CC-16 file scope (`mariana/api.py` slowapi import block) did not overlap the CC-22 file scope (four 404 sites in the same file, but at different line spans), so no manual conflict resolution was needed.)

---

## Numbers

- **HTTPException sites scrubbed:** 23 across 3 files (8 in `browser_server/app.py`, 11 in `sandbox_server/app.py`, 4 in `mariana/api.py`).
- **Tests updated:** 0.
- **pytest:** 513 passed / 11 skipped / 0 failed / 0 errors.
- **vitest:** 144/144 passed.
- **HEAD:** `a2ae37e`.

---

## Constraints honoured

- 0 bug tolerance: all three commits have green pytest + vitest before push.
- No `--force`: `git pull --rebase` then plain `git push`; remote was already at the right base.
- Did NOT touch CC-16 / CC-17 / CC-20 / CC-21 / CC-23 (parallel fixes by other subagents). CC-16 / CC-17 changes appearing in `git status` during this run were inspected, confirmed parallel-agent WIP, and left strictly alone — only `mariana/api.py` was staged for the CC-22 commit, and only the 4 audit-flagged sites were edited.
