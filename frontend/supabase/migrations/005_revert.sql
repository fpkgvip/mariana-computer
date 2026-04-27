-- 005_revert.sql — emergency revert for 005_loop6_b01_revoke_anon_rpcs.sql
-- ONLY use if the live deploy breaks because some service is unexpectedly
-- using the anon key for a credit/profile RPC. This restores the previous
-- (insecure) state.

BEGIN;

GRANT EXECUTE ON FUNCTION public.add_credits(uuid, integer)                    TO PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.deduct_credits(uuid, integer)                 TO PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_user_tokens(uuid)                         TO PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.check_balance(uuid)                           TO PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_stripe_customer_id(uuid)                  TO PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.update_profile_by_id(uuid, jsonb)             TO PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.update_profile_by_stripe_customer(text, jsonb)TO PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_set_credits(uuid, integer, boolean)     TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_adjust_credits(uuid, uuid, text, integer, text) TO PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_audit_insert(uuid, text, text, text, jsonb, jsonb, jsonb, text, text) TO PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_count_profiles()                        TO PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_list_profiles()                         TO PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.expire_credits()                              TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.spend_credits(uuid, integer, text, text, jsonb) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.grant_credits(uuid, integer, text, text, text, timestamptz) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.refund_credits(uuid, integer, text, text)     TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.handle_new_user()                             TO PUBLIC, anon, authenticated;

COMMIT;
