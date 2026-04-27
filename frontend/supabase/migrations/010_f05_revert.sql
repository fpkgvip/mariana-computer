-- 010_f05_revert.sql
-- Reverts migration 010_f05_research_tasks_owner_fk.sql.
-- Drops the FK constraint, the index, and the user_id column added by F-05.
--
-- WARNING: after this revert, deleting auth.users will no longer cascade
-- into research_tasks, and the orphan-row vulnerability (F-05) is restored.
-- Only use in development/rollback scenarios.

ALTER TABLE research_tasks DROP CONSTRAINT IF EXISTS research_tasks_user_id_fkey;
DROP INDEX IF EXISTS idx_research_tasks_user_id;
ALTER TABLE research_tasks DROP COLUMN IF EXISTS user_id;
