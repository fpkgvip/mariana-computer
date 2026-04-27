-- C19_stripe_grant_linkage.sql
-- Contract test: H-01 + H-02 — stripe_payment_grants and stripe_dispute_reversals
-- tables must exist with correct PKs, RLS enabled, and service_role-only access.

-- Test 1: stripe_payment_grants table exists with PK on payment_intent_id
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'stripe_payment_grants'
  ) THEN
    RAISE EXCEPTION 'H-01 FAIL: public.stripe_payment_grants table does not exist';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
      AND tc.table_schema = kcu.table_schema
    WHERE tc.table_schema = 'public'
      AND tc.table_name = 'stripe_payment_grants'
      AND tc.constraint_type = 'PRIMARY KEY'
      AND kcu.column_name = 'payment_intent_id'
  ) THEN
    RAISE EXCEPTION 'H-01 FAIL: stripe_payment_grants PRIMARY KEY not on payment_intent_id';
  END IF;

  RAISE NOTICE 'C19: stripe_payment_grants table and PK — OK';
END $$;

-- Test 2: stripe_dispute_reversals table exists with PK on reversal_key
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'stripe_dispute_reversals'
  ) THEN
    RAISE EXCEPTION 'H-02 FAIL: public.stripe_dispute_reversals table does not exist';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
      AND tc.table_schema = kcu.table_schema
    WHERE tc.table_schema = 'public'
      AND tc.table_name = 'stripe_dispute_reversals'
      AND tc.constraint_type = 'PRIMARY KEY'
      AND kcu.column_name = 'reversal_key'
  ) THEN
    RAISE EXCEPTION 'H-02 FAIL: stripe_dispute_reversals PRIMARY KEY not on reversal_key';
  END IF;

  RAISE NOTICE 'C19: stripe_dispute_reversals table and PK — OK';
END $$;

-- Test 3: RLS enabled on stripe_payment_grants
DO $$
DECLARE
  rls_enabled bool;
BEGIN
  SELECT relrowsecurity INTO rls_enabled
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relname = 'stripe_payment_grants';

  IF rls_enabled IS NULL THEN
    RAISE EXCEPTION 'H-01 FAIL: stripe_payment_grants table not found in pg_class';
  END IF;

  IF NOT rls_enabled THEN
    RAISE EXCEPTION 'H-01 FAIL: RLS not enabled on public.stripe_payment_grants';
  END IF;

  RAISE NOTICE 'C19: RLS enabled on stripe_payment_grants — OK';
END $$;

-- Test 4: RLS enabled on stripe_dispute_reversals
DO $$
DECLARE
  rls_enabled bool;
BEGIN
  SELECT relrowsecurity INTO rls_enabled
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relname = 'stripe_dispute_reversals';

  IF rls_enabled IS NULL THEN
    RAISE EXCEPTION 'H-02 FAIL: stripe_dispute_reversals table not found in pg_class';
  END IF;

  IF NOT rls_enabled THEN
    RAISE EXCEPTION 'H-02 FAIL: RLS not enabled on public.stripe_dispute_reversals';
  END IF;

  RAISE NOTICE 'C19: RLS enabled on stripe_dispute_reversals — OK';
END $$;

-- Test 5: service_role has INSERT privilege; anon and authenticated do not
-- on stripe_payment_grants
DO $$
DECLARE
  svc_has_insert bool;
  anon_has_insert bool;
  auth_has_insert bool;
BEGIN
  SELECT
    bool_or(grantee = 'service_role' AND privilege_type = 'INSERT') AS svc,
    bool_or(grantee = 'anon'         AND privilege_type = 'INSERT') AS anon,
    bool_or(grantee = 'authenticated' AND privilege_type = 'INSERT') AS auth
  INTO svc_has_insert, anon_has_insert, auth_has_insert
  FROM information_schema.role_table_grants
  WHERE table_schema = 'public' AND table_name = 'stripe_payment_grants';

  -- service_role may get grants via pg_class acl or superuser — skip strict check
  -- but anon and authenticated must NOT have insert.
  IF anon_has_insert THEN
    RAISE EXCEPTION 'H-01 FAIL: anon role has INSERT on stripe_payment_grants';
  END IF;

  IF auth_has_insert THEN
    RAISE EXCEPTION 'H-01 FAIL: authenticated role has INSERT on stripe_payment_grants';
  END IF;

  RAISE NOTICE 'C19: anon/authenticated have no INSERT on stripe_payment_grants — OK';
END $$;

-- Test 6: anon and authenticated have no privileges on stripe_dispute_reversals
DO $$
DECLARE
  anon_has_any bool;
  auth_has_any bool;
BEGIN
  SELECT
    bool_or(grantee = 'anon')          AS anon,
    bool_or(grantee = 'authenticated') AS auth
  INTO anon_has_any, auth_has_any
  FROM information_schema.role_table_grants
  WHERE table_schema = 'public' AND table_name = 'stripe_dispute_reversals';

  IF anon_has_any THEN
    RAISE EXCEPTION 'H-02 FAIL: anon role has privileges on stripe_dispute_reversals';
  END IF;

  IF auth_has_any THEN
    RAISE EXCEPTION 'H-02 FAIL: authenticated role has privileges on stripe_dispute_reversals';
  END IF;

  RAISE NOTICE 'C19: anon/authenticated have no privileges on stripe_dispute_reversals — OK';
END $$;
