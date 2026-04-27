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

    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- N-01: idempotent backfill for already-existing deployments.  CREATE TABLE
-- IF NOT EXISTS does not add columns to an already-present table, so we
-- explicitly ADD COLUMN IF NOT EXISTS for every column introduced after the
-- original M-01 fix.  Safe to run repeatedly and on fresh databases alike.
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS reserved_credits BIGINT NOT NULL DEFAULT 0;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS credits_settled BOOLEAN NOT NULL DEFAULT FALSE;

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
