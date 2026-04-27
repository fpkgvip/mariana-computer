-- @bug-id: R2
-- @sev: high
-- @phase: 0
-- @slice: contracts
-- @deterministic: must FAIL on baseline, PASS post-004b
--
-- R2: The unique idempotency index on credit_transactions must cover
-- type IN ('grant','refund','expiry'), not just 'grant'. This test asserts
-- a unique index named uq_credit_tx_idem exists with the right WHERE clause.

DO $$
DECLARE
  defn text;
BEGIN
  SELECT indexdef INTO defn FROM pg_indexes
   WHERE schemaname='public' AND indexname='uq_credit_tx_idem';

  IF defn IS NULL THEN
    RAISE EXCEPTION 'C03 FAIL: uq_credit_tx_idem not present';
  END IF;

  IF defn NOT ILIKE '%UNIQUE INDEX%' THEN
    RAISE EXCEPTION 'C03 FAIL: uq_credit_tx_idem is not UNIQUE: %', defn;
  END IF;

  -- Must cover refund and expiry (and grant).
  IF defn NOT ILIKE '%''grant''%' THEN
    RAISE EXCEPTION 'C03 FAIL: uq_credit_tx_idem missing grant: %', defn;
  END IF;
  IF defn NOT ILIKE '%''refund''%' THEN
    RAISE EXCEPTION 'C03 FAIL: uq_credit_tx_idem missing refund: %', defn;
  END IF;
  IF defn NOT ILIKE '%''expiry''%' THEN
    RAISE EXCEPTION 'C03 FAIL: uq_credit_tx_idem missing expiry: %', defn;
  END IF;
END $$;

SELECT 'C03 PASS: uq_credit_tx_idem covers grant+refund+expiry' AS result;
