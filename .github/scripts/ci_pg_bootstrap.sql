-- CI Postgres bootstrap — pre-baseline role setup.
-- The full schema (auth + public, including every table, view, function,
-- index, and policy that Loop 6 expects) lives in
-- ``ci_full_baseline.sql`` — generated from the local dev baseline that
-- mirrors NestD live.  This file ONLY creates the roles that the
-- baseline GRANT/REVOKE clauses reference (Supabase ships these by
-- default; stock PG does not), plus the pgcrypto extension that
-- ``gen_random_uuid()`` defaults need.

DO $$ BEGIN
  CREATE ROLE anon NOINHERIT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE ROLE authenticated NOINHERIT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE ROLE service_role NOINHERIT BYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
