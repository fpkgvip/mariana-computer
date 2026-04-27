-- =============================================================================
-- Migration: 004b_revert.sql
-- Purpose:   Revert 004b_credit_tx_idem_concurrent.sql.
-- Strategy:  CONCURRENT drop + CONCURRENT recreate of the narrow legacy index.
-- Order:     Apply BEFORE 004_revert.sql (this drops the wide index that
--            the wider WITH CHECK assertions in 004's _safe policy don't
--            depend on, but conceptually the index changes are reverted first).
-- =============================================================================

DROP INDEX CONCURRENTLY IF EXISTS public.uq_credit_tx_idem;

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_credit_tx_grant_ref
  ON public.credit_transactions (ref_type, ref_id)
  WHERE type = 'grant'
    AND ref_type IS NOT NULL
    AND ref_id IS NOT NULL;
