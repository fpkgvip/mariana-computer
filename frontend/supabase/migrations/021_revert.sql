-- Revert migration 021 — K-02 process_charge_reversal RPC.

BEGIN;

DROP FUNCTION IF EXISTS public.process_charge_reversal(
  uuid, text, text, text, text, integer, text, text
);

COMMIT;
