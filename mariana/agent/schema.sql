-- Mariana agent mode — database schema.
--
-- The table is created idempotently at orchestrator startup (same pattern
-- as ``mariana/data/db.py`` does for the research tables).

CREATE TABLE IF NOT EXISTS agent_tasks (
    id                       UUID PRIMARY KEY,
    user_id                  TEXT NOT NULL,
    conversation_id          TEXT,
    goal                     TEXT NOT NULL,
    user_instructions        TEXT,

    state                    TEXT NOT NULL,
    selected_model           TEXT NOT NULL DEFAULT 'claude-opus-4-7-20260208',
    steps                    JSONB NOT NULL DEFAULT '[]'::jsonb,
    artifacts                JSONB NOT NULL DEFAULT '[]'::jsonb,

    max_duration_hours       DOUBLE PRECISION NOT NULL DEFAULT 2.0,
    budget_usd               DOUBLE PRECISION NOT NULL DEFAULT 5.0,
    spent_usd                DOUBLE PRECISION NOT NULL DEFAULT 0.0,

    -- M-01 / N-01: agent credit reservation accounting.  ``reserved_credits``
    -- is the up-front Supabase deduction made by ``POST /api/agent``
    -- (canonical 100 credits/USD).  ``credits_settled`` is flipped to TRUE
    -- by ``_settle_agent_credits`` once the task reaches a terminal state
    -- and the refund / extra-deduct RPC has been attempted.  Persisting
    -- both columns is what makes settlement survive the queue-consumer
    -- reload path (mariana/main.py:_load_agent_task) and idempotent across
    -- requeue / crash recovery.
    reserved_credits         BIGINT NOT NULL DEFAULT 0,
    credits_settled          BOOLEAN NOT NULL DEFAULT FALSE,

    max_fix_attempts_per_step INTEGER NOT NULL DEFAULT 5,
    max_replans              INTEGER NOT NULL DEFAULT 3,
    replan_count             INTEGER NOT NULL DEFAULT 0,
    total_failures           INTEGER NOT NULL DEFAULT 0,

    final_answer             TEXT,

    stop_requested           BOOLEAN NOT NULL DEFAULT FALSE,
    error                    TEXT,

    -- U-03 fix: marks tasks that submitted a non-empty ``vault_env`` so
    -- the worker can fail-closed if the secret payload cannot be
    -- retrieved from Redis at fetch time.  Default FALSE so existing
    -- tasks (and tasks without a vault) carry no new Redis dependency.
    requires_vault           BOOLEAN NOT NULL DEFAULT FALSE,

    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- N-01: idempotent backfill for already-existing deployments.  CREATE TABLE
-- IF NOT EXISTS does not add columns to an already-present table, so we
-- explicitly ADD COLUMN IF NOT EXISTS for every column introduced after the
-- original M-01 fix.  Safe to run repeatedly and on fresh databases alike.
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS reserved_credits BIGINT NOT NULL DEFAULT 0;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS credits_settled BOOLEAN NOT NULL DEFAULT FALSE;
-- U-03 fix: idempotent backfill for already-existing deployments.
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS requires_vault BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_agent_tasks_user_id ON agent_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_state ON agent_tasks(state);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_created ON agent_tasks(created_at DESC);

-- Append-only log of every event the agent emits.  Used for the UI timeline
-- and for replay/debugging.  Not used for execution state — that lives in
-- agent_tasks.steps so updates are atomic.
CREATE TABLE IF NOT EXISTS agent_events (
    id              BIGSERIAL PRIMARY KEY,
    task_id         UUID NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    state           TEXT,
    step_id         TEXT,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_events_task_id ON agent_events(task_id, id);

-- R-01: DB-atomic settlement claim row.  Each agent_task can be settled at
-- most once; the (task_id) primary key plus INSERT...ON CONFLICT DO NOTHING
-- enforces this regardless of process-local in-memory flags or any race
-- between the stop endpoint, the worker's finally block, and a stale
-- requeue.  A successful Supabase add_credits/deduct_credits RPC stamps
-- ``completed_at``; if the RPC failed the row remains uncompleted but the
-- claim is locked, so a retry cannot mint a duplicate refund.  Operators
-- can reconcile uncompleted rows offline via the partial index below.
-- S-04: ON DELETE RESTRICT keeps settlement history immutable across task
-- UUID reuse (admin tooling, fixture reset, B-tree rebuild).  Operators
-- must explicitly drop the agent_settlements row before deleting the task.
-- S-02: CHECK (>= 0) on the credit columns is defense-in-depth against a
-- future caller persisting negative values.  delta_credits stays signed
-- (deliberately positive for overruns and negative for refunds).
CREATE TABLE IF NOT EXISTS agent_settlements (
    task_id           UUID PRIMARY KEY REFERENCES agent_tasks(id) ON DELETE RESTRICT,
    user_id           TEXT NOT NULL,
    reserved_credits  BIGINT NOT NULL CHECK (reserved_credits >= 0),
    final_credits     BIGINT NOT NULL CHECK (final_credits >= 0),
    delta_credits     BIGINT NOT NULL,
    ref_id            TEXT NOT NULL,
    claimed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);

-- S-02 / S-04: idempotent backfill for already-existing deployments where
-- the table was created with the older constraint set.  Drop-then-add
-- under IF EXISTS so a fresh database is unaffected.
ALTER TABLE agent_settlements
    DROP CONSTRAINT IF EXISTS agent_settlements_reserved_credits_check;
ALTER TABLE agent_settlements
    ADD CONSTRAINT agent_settlements_reserved_credits_check
        CHECK (reserved_credits >= 0);
ALTER TABLE agent_settlements
    DROP CONSTRAINT IF EXISTS agent_settlements_final_credits_check;
ALTER TABLE agent_settlements
    ADD CONSTRAINT agent_settlements_final_credits_check
        CHECK (final_credits >= 0);
ALTER TABLE agent_settlements
    DROP CONSTRAINT IF EXISTS agent_settlements_task_id_fkey;
ALTER TABLE agent_settlements
    ADD CONSTRAINT agent_settlements_task_id_fkey
        FOREIGN KEY (task_id) REFERENCES agent_tasks(id) ON DELETE RESTRICT;

-- T-01: separate "ledger RPC has been applied" from "settlement workflow
-- complete".  ``completed_at`` previously conflated the two: a successful
-- ledger RPC followed by a transient failure to stamp ``completed_at``
-- left the row eligible for reconciler retry, which then re-issued the
-- ledger RPC against non-idempotent ``add_credits`` / ``deduct_credits``
-- and caused double-settlement (refund-twice or charge-twice).
--
-- The fix routes settlement through the idempotent ledger primitives
-- ``grant_credits(p_source='refund', p_ref_type='agent_task', p_ref_id=task.id)``
-- and ``refund_credits(p_ref_type='agent_task_overrun', p_ref_id=task.id)``
-- (live in NestD; both dedupe on ``(ref_type, ref_id)`` against
-- ``credit_transactions``), AND it stamps ``ledger_applied_at`` BEFORE
-- ``completed_at`` so the reconciler can distinguish "ledger mutation
-- already on disk, just finish the bookkeeping" from "ledger genuinely
-- failed, retry the RPC".  Defense-in-depth: even if both markers fail
-- to land, the next RPC would be deduplicated by the ledger.
ALTER TABLE agent_settlements
    ADD COLUMN IF NOT EXISTS ledger_applied_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_agent_settlements_completed
    ON agent_settlements(completed_at) WHERE completed_at IS NULL;

-- T-01: index on rows that need bookkeeping cleanup — ledger applied
-- but completed_at not yet stamped.  The reconciler short-cuts these
-- without re-issuing the ledger RPC.
CREATE INDEX IF NOT EXISTS idx_agent_settlements_ledger_applied_pending_complete
    ON agent_settlements(ledger_applied_at)
    WHERE completed_at IS NULL AND ledger_applied_at IS NOT NULL;
