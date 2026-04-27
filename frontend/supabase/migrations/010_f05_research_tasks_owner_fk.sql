-- 010_f05_research_tasks_owner_fk.sql
-- F-05 fix: add relational user_id FK to research_tasks so that deleting
-- a user from auth.users cascades through to research_tasks and all its
-- descendant rows, eliminating the orphan-row problem found in the Phase E
-- re-audit.
--
-- Strategy:
--   1. Add nullable user_id column (allows backfill without locking prod).
--   2. Backfill from metadata->>'user_id'.
--   3. Create index.
--   4. Add FK constraint to auth.users ON DELETE CASCADE.
--   5. Make NOT NULL only if zero NULLs remain after backfill.
--      Otherwise keep nullable and emit a DO-block warning with the count.
--
-- Descendant tables (hypotheses, findings, sources, branches, checkpoints,
-- tribunal_sessions, skeptic_results, report_generations, evaluation_results,
-- claims, source_scores, contradiction_pairs, research_plans, hypothesis_priors,
-- gap_analyses, perspective_syntheses, audit_results, executive_summaries,
-- learning_events, investigation_outcomes) already carry
-- "REFERENCES research_tasks(id) ON DELETE CASCADE" in db.py, so the cascade
-- from auth.users -> research_tasks -> all descendants is complete once this
-- migration lands.
--
-- NOTE: research_tasks lives in the *backend* asyncpg schema (not public.*),
-- so this migration targets that schema directly.  When running locally via
-- psql the table is in the default schema.  On NestD the backend connects to
-- the same Postgres instance via the service-role key and all tables are in
-- the public schema.
--
-- SAFETY: The entire migration is wrapped in a table-existence guard.
-- If research_tasks does not yet exist (e.g. backend has not run init_schema()
-- yet), this migration is a safe no-op.  The db.py schema already includes the
-- user_id FK column, so when init_schema() runs it will create the table
-- correctly from scratch.  This migration exists to handle the case where an
-- older backend already created research_tasks without the column.

DO $$
DECLARE
    table_exists  boolean;
    col_exists    boolean;
    fk_exists     boolean;
    idx_exists    boolean;
    null_count    bigint;
BEGIN
    -- Guard: only run if research_tasks already exists in public schema.
    SELECT EXISTS (
        SELECT 1 FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename  = 'research_tasks'
    ) INTO table_exists;

    IF NOT table_exists THEN
        RAISE NOTICE 'F-05: research_tasks does not yet exist in public schema. '
                     'Migration is a no-op; init_schema() will create the table '
                     'with user_id FK column included.';
        RETURN;  -- exit the DO block cleanly
    END IF;

    -- ── Step 1: add the nullable column ──────────────────────────────────────
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'research_tasks'
          AND column_name  = 'user_id'
    ) INTO col_exists;

    IF NOT col_exists THEN
        EXECUTE 'ALTER TABLE research_tasks ADD COLUMN user_id UUID';
        RAISE NOTICE 'F-05: user_id column added to research_tasks.';
    ELSE
        RAISE NOTICE 'F-05: user_id column already exists on research_tasks, skipping ADD COLUMN.';
    END IF;

    -- ── Step 2: add FK constraint (NOT VALID for cheap creation) ─────────────
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname  = 'research_tasks_user_id_fkey'
          AND conrelid = 'public.research_tasks'::regclass
    ) INTO fk_exists;

    IF NOT fk_exists THEN
        EXECUTE '
            ALTER TABLE research_tasks
                ADD CONSTRAINT research_tasks_user_id_fkey
                FOREIGN KEY (user_id)
                REFERENCES auth.users(id)
                ON DELETE CASCADE
                NOT VALID';
        RAISE NOTICE 'F-05: FK constraint research_tasks_user_id_fkey added (NOT VALID).';
    ELSE
        RAISE NOTICE 'F-05: FK constraint already exists, skipping.';
    END IF;

    -- ── Step 3: backfill from metadata JSONB ─────────────────────────────────
    EXECUTE '
        UPDATE research_tasks
        SET    user_id = (metadata->>''user_id'')::uuid
        WHERE  user_id IS NULL
          AND  metadata->>''user_id'' IS NOT NULL
          AND  (metadata->>''user_id'') ~ ''^[0-9a-fA-F-]{36}$''';

    -- ── Step 4: validate the FK ───────────────────────────────────────────────
    EXECUTE 'ALTER TABLE research_tasks VALIDATE CONSTRAINT research_tasks_user_id_fkey';

    -- ── Step 5: create index ──────────────────────────────────────────────────
    SELECT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'research_tasks'
          AND indexname  = 'idx_research_tasks_user_id'
    ) INTO idx_exists;

    IF NOT idx_exists THEN
        EXECUTE 'CREATE INDEX idx_research_tasks_user_id ON research_tasks(user_id)';
        RAISE NOTICE 'F-05: index idx_research_tasks_user_id created.';
    ELSE
        RAISE NOTICE 'F-05: index already exists, skipping.';
    END IF;

    -- ── Step 6: make NOT NULL only when backfill is clean ────────────────────
    EXECUTE 'SELECT count(*) FROM research_tasks WHERE user_id IS NULL'
        INTO null_count;

    IF null_count = 0 THEN
        EXECUTE 'ALTER TABLE research_tasks ALTER COLUMN user_id SET NOT NULL';
        RAISE NOTICE 'F-05: user_id column set NOT NULL (0 NULL rows after backfill).';
    ELSE
        RAISE WARNING 'F-05: % research_tasks row(s) still have NULL user_id after backfill. '
            'Column kept nullable. Investigate legacy/bad-data rows before adding NOT NULL.',
            null_count;
    END IF;

END $$;
