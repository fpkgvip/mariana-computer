-- ============================================================
-- Revert 013: drop FK covering indexes added by B-32
-- ============================================================

DROP INDEX IF EXISTS public.idx_investigations_user_id;
DROP INDEX IF EXISTS public.idx_credit_tx_bucket_id;
DROP INDEX IF EXISTS public.idx_system_status_frozen_by;
DROP INDEX IF EXISTS public.idx_admin_tasks_assigned_to;
DROP INDEX IF EXISTS public.idx_admin_tasks_created_by;
DROP INDEX IF EXISTS public.idx_feature_flags_updated_by;
DROP INDEX IF EXISTS public.idx_messages_investigation_id;
