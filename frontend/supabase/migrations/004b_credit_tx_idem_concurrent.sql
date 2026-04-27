-- =============================================================================
-- Migration: 004b_credit_tx_idem_concurrent.sql
-- Purpose:   Widen credit_transactions idempotency index from grant-only to
--            cover (grant, refund, expiry).
-- Strategy:  CREATE INDEX CONCURRENTLY — no transaction wrapper, no lock.
-- Order:     Apply AFTER 004 has run successfully.
-- Reverter:  004_revert.sql restores uq_credit_tx_grant_ref (also CONCURRENT).
--
-- NOTE: spend_credits is intentionally excluded. It writes one transaction
-- per bucket per spend call (FIFO debit), so multiple rows can legitimately
-- share the same (ref_type, ref_id, type='spend').
--
-- Pre-flight asserted by 004's pre-flight: zero existing duplicates for
-- (ref_type, ref_id, type) where type IN ('grant','refund','expiry') and
-- ref_type/ref_id NOT NULL. Verified against live (project NestD) on
-- 2026-04-27: 0 duplicate groups.
-- =============================================================================

-- This MUST be at file-level (no transaction). Supabase CLI/MCP applies it
-- as a single statement.

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_credit_tx_idem
  ON public.credit_transactions (ref_type, ref_id, type)
  WHERE type IN ('grant', 'refund', 'expiry')
    AND ref_type IS NOT NULL
    AND ref_id IS NOT NULL;
