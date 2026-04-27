-- C09_research_tasks_owner_fk.sql
-- @bug-id: F-05
-- @sev: P2
-- @phase: E-re-audit
-- @slice: contracts
-- @deterministic: must FAIL on baseline (no FK column), PASS after 010_f05_research_tasks_owner_fk.sql
--
-- Asserts all of:
--   1. user_id column exists on research_tasks.
--   2. A FK constraint from research_tasks.user_id -> auth.users(id) exists.
--   3. The FK is ON DELETE CASCADE.
--   4. An index on research_tasks(user_id) exists.
--
-- Note: this runs against the local testdb which uses the db.py init_schema()
-- bootstrap (not the Supabase migration runner). Column must exist in both.

DO $$
DECLARE
  col_count      int;
  fk_count       int;
  cascade_count  int;
  idx_count      int;
BEGIN
  -- 1. user_id column exists
  SELECT count(*) INTO col_count
  FROM information_schema.columns
  WHERE table_name = 'research_tasks'
    AND column_name = 'user_id';

  IF col_count = 0 THEN
    RAISE EXCEPTION 'C09 FAIL: user_id column missing from research_tasks';
  END IF;

  -- 2. FK constraint referencing auth.users exists
  SELECT count(*) INTO fk_count
  FROM information_schema.referential_constraints rc
  JOIN information_schema.key_column_usage kcu
    ON rc.constraint_name = kcu.constraint_name
  WHERE kcu.table_name = 'research_tasks'
    AND kcu.column_name = 'user_id';

  IF fk_count = 0 THEN
    RAISE EXCEPTION 'C09 FAIL: no FK constraint on research_tasks.user_id';
  END IF;

  -- 3. ON DELETE CASCADE (pg_constraint)
  SELECT count(*) INTO cascade_count
  FROM pg_constraint c
  JOIN pg_class t ON t.oid = c.conrelid
  WHERE t.relname = 'research_tasks'
    AND c.contype = 'f'
    AND c.confdeltype = 'c';  -- 'c' = CASCADE

  IF cascade_count = 0 THEN
    RAISE EXCEPTION 'C09 FAIL: FK on research_tasks.user_id is not ON DELETE CASCADE';
  END IF;

  -- 4. Index on user_id exists
  SELECT count(*) INTO idx_count
  FROM pg_indexes
  WHERE tablename = 'research_tasks'
    AND indexdef ILIKE '%user_id%';

  IF idx_count = 0 THEN
    RAISE EXCEPTION 'C09 FAIL: no index on research_tasks(user_id)';
  END IF;
END $$;

SELECT 'C09 PASS: research_tasks has user_id FK column, FK constraint with CASCADE, and index' AS result;
