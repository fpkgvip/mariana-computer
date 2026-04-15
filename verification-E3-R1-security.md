# Verification E3-R1 Security Review
2026-04-15

## Result: FAIL — 2 new vulnerabilities

## Vulnerabilities Found
### SEC-E3-R1-01: SSE authentication leaks full bearer JWT in URL query string
- File: /home/user/workspace/mariana/frontend/src/pages/Chat.tsx; /home/user/workspace/mariana/mariana/api.py
- Lines: Chat.tsx 767-783; api.py 546-560, 1217-1235
- Severity: High
- Attack vector: When a user opens the live log stream, the frontend constructs an EventSource URL of the form `/api/investigations/{task_id}/logs?token=<JWT>`. The backend explicitly accepts authentication from the `token` query parameter. That exposes the full bearer token anywhere request URLs are recorded: reverse-proxy access logs, CDN logs, APM/network tracing, browser network tooling, and any monitoring that stores full URLs. An attacker who can read those logs can replay the stolen JWT against normal authenticated API endpoints and impersonate the victim until the token expires.
- Fix: Remove query-parameter bearer authentication for SSE. Use a transport that supports Authorization headers (for example fetch + ReadableStream), or mint a short-lived single-purpose stream token that is not accepted by any other endpoint. Also redact sensitive query parameters from logs as defense in depth.

### SEC-E3-R1-02: Parallel upload requests bypass the 5-file cap and allow storage exhaustion
- File: /home/user/workspace/mariana/mariana/api.py
- Lines: 1689-1704, 1789-1797
- Severity: Medium
- Attack vector: The existing-investigation upload path and the pending-session upload path both enforce the file cap with a non-atomic `existing_count + len(files)` check before writing files. An authenticated attacker can send multiple upload requests to the same task or pending session in parallel. Each request observes the same pre-write file count, all requests pass validation, and all then write files. This bypasses the intended 5-file limit and allows significantly more than the intended storage quota, creating a practical authenticated DoS vector.
- Fix: Enforce the cap atomically. Examples: per-task/session locks around the count-and-write section, a transactional metadata counter in the database, or staging uploads and committing only if the final count still fits within the limit.
