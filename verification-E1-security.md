# Verification E1 Security Review

New vulnerabilities found in this round.

## 1) Cross-user custom skill overwrite via shared filename namespace
- **File:** `mariana/tools/skills.py`
- **Lines:** 177-214, 228-245
- **Severity:** Medium
- **Attack vector:** Custom skill IDs are derived only from the sanitized skill name (`custom-{name}`), then written directly to a global shared `DATA_ROOT/skills/` directory with `write_text(...)`. There is no per-user namespace, no ownership check before overwrite, and no collision prevention. A malicious user can create a skill with the same name as another user's skill and silently replace that file, changing `owner_id`, `description`, `system_prompt`, and `trigger_keywords`. This is a tenant-isolation failure affecting integrity and availability of other users' custom skills.
- **Why this is new:** The current delete path checks ownership, but create does not; the overwrite happens before any ownership check can protect the victim.
- **Fix:** Namespace skill files per owner (for example `skills/{owner_id}/{skill_id}.json`) or generate random immutable IDs instead of name-derived IDs. On create, reject collisions when an existing skill belongs to another owner.

## 2) Pending upload session hijack because `session_uuid` is not bound to the authenticated user
- **File:** `mariana/api.py`
- **Lines:** 802-823, 1633-1638, 1736-1806
- **Severity:** Medium
- **Attack vector:** Pre-submission uploads are stored under `uploads/pending/{session_uuid}` and later moved into a task when `upload_session_uuid` is supplied to `POST /api/investigations`. The only validation is “is this a UUID”; there is no server-side binding between the session UUID and the authenticated user. Any authenticated user who learns another user's pending upload UUID can: (a) upload additional files into that session with `POST /api/upload`, or (b) claim that pending directory into their own investigation by supplying the victim's `upload_session_uuid`. That enables unauthorized file injection and file theft across users.
- **Why this is new:** Upload auth exists, but ownership of the pending upload session itself is never enforced.
- **Fix:** Persist upload-session ownership server-side (DB row or signed metadata file) and verify `current_user["user_id"]` on both upload and task-creation move. Expire and garbage-collect old pending sessions.

## 3) Absolute server file paths are disclosed to investigation owners when output files are missing
- **File:** `mariana/api.py`
- **Lines:** 1396-1400, 1452-1456
- **Severity:** Low
- **Attack vector:** If a task record contains `output_pdf_path` or `output_docx_path` but the file is absent on disk, the API returns `detail=f"PDF file not found on disk: {pdf_path}"` and the DOCX equivalent. That leaks internal absolute filesystem paths to end users. This is a new information-disclosure path distinct from the already-fixed config/connectors redaction work.
- **Fix:** Return a generic message such as `Report file not available` and keep the real path only in server logs.
