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
