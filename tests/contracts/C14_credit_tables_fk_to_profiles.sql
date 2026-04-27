-- @bug-id: B-15
-- @sev: P2
-- @phase: 6
-- @slice: contracts
-- @deterministic: must FAIL on baseline (FKs point to auth.users),
--                 must PASS post-011 (FKs point to public.profiles with CASCADE).
--
-- B-15: credit_buckets.user_id and credit_transactions.user_id MUST have
-- FK constraints referencing public.profiles(id) ON DELETE CASCADE,
-- NOT auth.users(id).

DO $$
DECLARE
  bad  text := '';
  r    record;
  tables_cols text[][] := ARRAY[
    ARRAY['credit_buckets',      'user_id', 'credit_buckets_user_id_fkey'],
    ARRAY['credit_transactions', 'user_id', 'credit_transactions_user_id_fkey']
  ];
  pair text[];
  tbl  text;
  col  text;
  cname text;
BEGIN
  FOREACH pair SLICE 1 IN ARRAY tables_cols LOOP
    tbl   := pair[1];
    col   := pair[2];
    cname := pair[3];

    -- Check constraint exists
    IF NOT EXISTS (
      SELECT 1 FROM pg_constraint c
        JOIN pg_class cl ON c.conrelid = cl.oid
        JOIN pg_namespace n ON cl.relnamespace = n.oid
       WHERE c.contype = 'f'
         AND n.nspname = 'public'
         AND cl.relname = tbl
         AND c.conname = cname
    ) THEN
      bad := bad || format(
        E'  %s: FK constraint %s not found\n', tbl, cname
      );
      CONTINUE;
    END IF;

    -- Check it references public.profiles (not auth.users)
    SELECT
      confrelid::regclass::text AS foreign_table,
      pg_get_constraintdef(c.oid) AS def
    INTO r
    FROM pg_constraint c
      JOIN pg_class cl ON c.conrelid = cl.oid
      JOIN pg_namespace n ON cl.relnamespace = n.oid
    WHERE c.contype = 'f'
      AND n.nspname = 'public'
      AND cl.relname = tbl
      AND c.conname = cname;

    IF r.foreign_table <> 'profiles' THEN
      bad := bad || format(
        E'  %s.%s: FK references %s — must reference public.profiles(id)\n',
        tbl, col, r.foreign_table
      );
    END IF;

    -- Check ON DELETE CASCADE
    IF r.def NOT ILIKE '%ON DELETE CASCADE%' THEN
      bad := bad || format(
        E'  %s.%s: FK does not have ON DELETE CASCADE (got: %s)\n',
        tbl, col, r.def
      );
    END IF;

    -- Verify no reference to auth.users remains
    IF r.def ILIKE '%auth.users%' THEN
      bad := bad || format(
        E'  %s.%s: FK definition still mentions auth.users: %s\n',
        tbl, col, r.def
      );
    END IF;
  END LOOP;

  IF length(bad) > 0 THEN
    RAISE EXCEPTION E'C14 FAIL: credit table FK posture violations:\n%', bad;
  END IF;
END $$;

SELECT 'C14 PASS: credit_buckets and credit_transactions FK user_id → public.profiles ON DELETE CASCADE' AS result;
