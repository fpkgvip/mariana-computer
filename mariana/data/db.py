"""
Mariana Computer — AsyncPG database layer.

Provides:
  - ``create_pool()``      — opens an asyncpg connection pool.
  - ``init_schema()``      — idempotently creates all tables.
  - CRUD helpers for every entity in models.py.

All SQL is parameterised; no f-strings or string interpolation are ever used
inside query bodies to prevent SQL injection.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from mariana.data.models import (
    AISession,
    Branch,
    BranchStatus,
    Checkpoint,
    EvidenceType,
    Finding,
    Hypothesis,
    HypothesisStatus,
    ResearchTask,
    SkepticResult,
    Source,
    SourceType,
    State,
    TaskStatus,
    TribunalSession,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BUG-007: Column allowlists at module level so they cannot be accidentally
# shadow-overridden by local variables inside function bodies.
# ---------------------------------------------------------------------------

_ALLOWED_TASK_COLUMNS: frozenset[str] = frozenset({
    "topic", "budget_usd", "status", "current_state", "total_spent_usd",
    "diminishing_flags", "ai_call_counter", "started_at", "completed_at",
    "error_message", "output_pdf_path", "output_docx_path", "metadata",
    # BUG-D1-11 fix: include dedicated schema columns so update_research_task can set them
    "quality_tier", "user_flow_instructions", "continuous_mode", "dont_kill_branches",
})

_ALLOWED_BRANCH_COLUMNS: frozenset[str] = frozenset({
    "hypothesis_id", "task_id", "status", "score_history", "budget_allocated",
    "budget_spent", "grants_log", "cycles_completed", "kill_reason",
    "sources_searched", "updated_at",
})


# ---------------------------------------------------------------------------
# Pool creation
# ---------------------------------------------------------------------------


async def create_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 10,
    command_timeout: float = 60.0,
) -> asyncpg.Pool:
    """
    Open and return an asyncpg connection pool.

    Args:
        dsn:             PostgreSQL DSN string.
        min_size:        Minimum number of connections to keep open.
        max_size:        Maximum number of connections in the pool.
        command_timeout: Default per-query timeout in seconds.

    Returns:
        A connected :class:`asyncpg.Pool` instance.
    """
    pool: asyncpg.Pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=command_timeout,
    )
    logger.info("AsyncPG pool created (min=%d, max=%d)", min_size, max_size)
    return pool


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
-- BUG-NEW-10 fix: ensure pgcrypto extension is available for gen_random_uuid()
-- used in report_generations and evaluation_results tables.
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS research_tasks (
    id                  TEXT        PRIMARY KEY,
    topic               TEXT        NOT NULL,
    budget_usd          NUMERIC     NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'PENDING',
    current_state       TEXT        NOT NULL DEFAULT 'INIT',
    total_spent_usd     NUMERIC     NOT NULL DEFAULT 0,
    diminishing_flags   INTEGER     NOT NULL DEFAULT 0,
    ai_call_counter     INTEGER     NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    error_message       TEXT,
    output_pdf_path     TEXT,
    output_docx_path    TEXT,
    metadata            JSONB       NOT NULL DEFAULT '{}',
    quality_tier        TEXT        DEFAULT 'balanced',
    user_flow_instructions TEXT     DEFAULT '',
    continuous_mode     BOOLEAN     DEFAULT FALSE,
    dont_kill_branches  BOOLEAN     DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id              TEXT        PRIMARY KEY,
    task_id         TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    parent_id       TEXT        REFERENCES hypotheses(id) ON DELETE SET NULL,
    depth           INTEGER     NOT NULL DEFAULT 0,
    statement       TEXT        NOT NULL,
    statement_zh    TEXT,
    status          TEXT        NOT NULL DEFAULT 'PENDING',
    score           NUMERIC,
    momentum_note   TEXT,
    rationale       TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hypotheses_task_id ON hypotheses(task_id);
CREATE INDEX IF NOT EXISTS idx_hypotheses_parent_id ON hypotheses(parent_id);

CREATE TABLE IF NOT EXISTS findings (
    id               TEXT        PRIMARY KEY,
    task_id          TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    hypothesis_id    TEXT        NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
    content          TEXT        NOT NULL,
    content_en       TEXT,
    content_language TEXT        NOT NULL DEFAULT 'en',
    source_ids       JSONB       NOT NULL DEFAULT '[]',
    confidence       NUMERIC     NOT NULL DEFAULT 0.5,
    evidence_type    TEXT        NOT NULL DEFAULT 'NEUTRAL',
    is_compressed    BOOLEAN     NOT NULL DEFAULT FALSE,
    raw_content_path TEXT,
    created_at       TIMESTAMPTZ NOT NULL,
    metadata         JSONB       NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_findings_task_id      ON findings(task_id);
CREATE INDEX IF NOT EXISTS idx_findings_hypothesis_id ON findings(hypothesis_id);

CREATE TABLE IF NOT EXISTS sources (
    id           TEXT        PRIMARY KEY,
    task_id      TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    url          TEXT        NOT NULL,
    url_hash     TEXT        NOT NULL,
    title        TEXT,
    title_en     TEXT,
    content_hash TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL,
    cache_expiry TIMESTAMPTZ,
    source_type  TEXT        NOT NULL DEFAULT 'NEWS',
    language     TEXT        NOT NULL DEFAULT 'en',
    adapter_name TEXT,
    is_paywalled BOOLEAN     NOT NULL DEFAULT FALSE,
    metadata     JSONB       NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sources_task_id  ON sources(task_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_url_hash ON sources(task_id, url_hash);

CREATE TABLE IF NOT EXISTS ai_sessions (
    id                    TEXT        PRIMARY KEY,
    task_id               TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    branch_id             TEXT,
    task_type             TEXT        NOT NULL,
    model_used            TEXT        NOT NULL,
    input_tokens          INTEGER     NOT NULL DEFAULT 0,
    output_tokens         INTEGER     NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER     NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER     NOT NULL DEFAULT 0,
    cost_usd              NUMERIC     NOT NULL DEFAULT 0,
    duration_ms           INTEGER     NOT NULL DEFAULT 0,
    used_batch_api        BOOLEAN     NOT NULL DEFAULT FALSE,
    batch_id              TEXT,
    cache_hit             BOOLEAN     NOT NULL DEFAULT FALSE,
    started_at            TIMESTAMPTZ NOT NULL,
    error                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_ai_sessions_task_id   ON ai_sessions(task_id);
CREATE INDEX IF NOT EXISTS idx_ai_sessions_branch_id ON ai_sessions(branch_id);

CREATE TABLE IF NOT EXISTS branches (
    id               TEXT        PRIMARY KEY,
    hypothesis_id    TEXT        NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
    task_id          TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    status           TEXT        NOT NULL DEFAULT 'ACTIVE',
    score_history    JSONB       NOT NULL DEFAULT '[]',
    budget_allocated NUMERIC     NOT NULL DEFAULT 5.0,
    budget_spent     NUMERIC     NOT NULL DEFAULT 0,
    grants_log       JSONB       NOT NULL DEFAULT '[]',
    cycles_completed INTEGER     NOT NULL DEFAULT 0,
    kill_reason      TEXT,
    sources_searched JSONB       NOT NULL DEFAULT '[]',
    created_at       TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_branches_task_id      ON branches(task_id);
CREATE INDEX IF NOT EXISTS idx_branches_hypothesis_id ON branches(hypothesis_id);
CREATE INDEX IF NOT EXISTS idx_branches_status        ON branches(status);

CREATE TABLE IF NOT EXISTS checkpoints (
    id                   TEXT        PRIMARY KEY,
    task_id              TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    timestamp            TIMESTAMPTZ NOT NULL,
    state_machine_state  TEXT        NOT NULL,
    active_branch_ids    JSONB       NOT NULL DEFAULT '[]',
    killed_branch_ids    JSONB       NOT NULL DEFAULT '[]',
    compressed_findings  JSONB       NOT NULL DEFAULT '[]',
    budget_remaining     NUMERIC     NOT NULL,
    total_spent          NUMERIC     NOT NULL,
    diminishing_flags    INTEGER     NOT NULL DEFAULT 0,
    ai_call_counter      INTEGER     NOT NULL DEFAULT 0,
    snapshot_path        TEXT,
    diminishing_result   TEXT
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_task_id ON checkpoints(task_id);

CREATE TABLE IF NOT EXISTS tribunal_sessions (
    id                              TEXT        PRIMARY KEY,
    task_id                         TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    finding_id                      TEXT        NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    plaintiff_args                  TEXT,
    defendant_args                  TEXT,
    plaintiff_rebuttal              TEXT,
    defendant_counter               TEXT,
    verdict                         TEXT,
    judge_plaintiff_score           NUMERIC,
    judge_defendant_score           NUMERIC,
    judge_reasoning                 TEXT,
    unanswered_questions            JSONB       NOT NULL DEFAULT '[]',
    total_cost_usd                  NUMERIC     NOT NULL DEFAULT 0,
    created_at                      TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tribunal_sessions_task_id    ON tribunal_sessions(task_id);
CREATE INDEX IF NOT EXISTS idx_tribunal_sessions_finding_id ON tribunal_sessions(finding_id);

CREATE TABLE IF NOT EXISTS skeptic_results (
    id                         TEXT        PRIMARY KEY,
    task_id                    TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    finding_id                 TEXT        NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    tribunal_session_id        TEXT        REFERENCES tribunal_sessions(id) ON DELETE SET NULL,
    questions                  JSONB       NOT NULL DEFAULT '[]',
    open_count                 INTEGER     NOT NULL DEFAULT 0,
    researchable_count         INTEGER     NOT NULL DEFAULT 0,
    resolved_count             INTEGER     NOT NULL DEFAULT 0,
    critical_open_count        INTEGER     NOT NULL DEFAULT 0,
    passes_publishing_threshold BOOLEAN    NOT NULL DEFAULT FALSE,
    cost_usd                   NUMERIC     NOT NULL DEFAULT 0,
    created_at                 TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skeptic_results_task_id    ON skeptic_results(task_id);
CREATE INDEX IF NOT EXISTS idx_skeptic_results_finding_id ON skeptic_results(finding_id);

CREATE TABLE IF NOT EXISTS report_generations (
    id              TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    task_id         TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    pdf_path        TEXT,
    docx_path       TEXT,
    report_cost_usd NUMERIC    NOT NULL DEFAULT 0,
    generated_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_report_generations_task_id ON report_generations(task_id);

CREATE TABLE IF NOT EXISTS evaluation_results (
    id          TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    task_id     TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    branch_id   TEXT        NOT NULL,
    score       NUMERIC     NOT NULL,
    reasoning   TEXT,
    next_search_keywords JSONB DEFAULT '[]',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_evaluation_results_task_id ON evaluation_results(task_id);
CREATE INDEX IF NOT EXISTS idx_evaluation_results_branch_id ON evaluation_results(branch_id);

CREATE TABLE IF NOT EXISTS stripe_webhook_events (
    event_id     TEXT        PRIMARY KEY,
    event_type   TEXT        NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stripe_webhook_events_processed_at ON stripe_webhook_events(processed_at);

CREATE TABLE IF NOT EXISTS graph_nodes (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'entity',
    description TEXT DEFAULT '',
    metadata JSONB DEFAULT '{}',
    x DOUBLE PRECISION,
    y DOUBLE PRECISION,
    created_at TIMESTAMPTZ DEFAULT now(),
    source TEXT DEFAULT 'ai'
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_task ON graph_nodes(task_id);

CREATE TABLE IF NOT EXISTS graph_edges (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    source_node TEXT NOT NULL,
    target_node TEXT NOT NULL,
    label TEXT DEFAULT '',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    source TEXT DEFAULT 'ai'
);
CREATE INDEX IF NOT EXISTS idx_graph_edges_task ON graph_edges(task_id);

CREATE TABLE IF NOT EXISTS orchestrator_handoffs (
    id          TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::text,
    task_id     TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    phase       TEXT        NOT NULL,
    context     JSONB       NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_handoffs_task ON orchestrator_handoffs(task_id);
"""


async def init_schema(pool: asyncpg.Pool) -> None:
    """
    Idempotently create all database tables and indices.

    Safe to call on every application start-up.  Uses ``CREATE TABLE IF NOT
    EXISTS`` and ``CREATE INDEX IF NOT EXISTS`` throughout.

    Args:
        pool: An open asyncpg connection pool.
    """
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    logger.info("Database schema initialised (or already present)")


# ---------------------------------------------------------------------------
# Helper: row → dict conversion
# ---------------------------------------------------------------------------


# Columns that are stored as JSONB in PostgreSQL and should be JSON-decoded.
_JSON_COLUMNS: frozenset[str] = frozenset({
    # research_tasks
    "metadata",
    # findings
    "source_ids",
    # branches
    "score_history",
    "grants_log",
    "sources_searched",
    # checkpoints
    "active_branch_ids",
    "killed_branch_ids",
    "compressed_findings",
    # tribunal_sessions
    "unanswered_questions",
    # skeptic_results
    "questions",
    # evaluation_results
    "next_search_keywords",
})


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """Convert an asyncpg Record to a plain dict, decoding known JSON columns."""
    result: dict[str, Any] = {}
    for key, value in row.items():
        if key in _JSON_COLUMNS and isinstance(value, str):
            # Only attempt JSON decoding for columns known to be JSONB
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# ResearchTask CRUD
# ---------------------------------------------------------------------------


async def insert_research_task(pool: asyncpg.Pool, task: ResearchTask) -> None:
    """Insert a new ResearchTask record."""
    async with pool.acquire() as conn:
        # BUG-D1-03 fix: include quality_tier, user_flow_instructions, continuous_mode,
        # dont_kill_branches so their dedicated DB columns are populated at INSERT time.
        # These are read from task.metadata (set by the API before calling insert).
        _meta = task.metadata or {}
        await conn.execute(
            """
            INSERT INTO research_tasks (
                id, topic, budget_usd, status, current_state,
                total_spent_usd, diminishing_flags, ai_call_counter,
                created_at, started_at, completed_at, error_message,
                output_pdf_path, output_docx_path, metadata,
                quality_tier, user_flow_instructions,
                continuous_mode, dont_kill_branches
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8,
                $9, $10, $11, $12,
                $13, $14, $15,
                $16, $17,
                $18, $19
            )
            ON CONFLICT (id) DO NOTHING
            """,
            task.id,
            task.topic,
            task.budget_usd,
            task.status.value,
            task.current_state.value,
            task.total_spent_usd,
            task.diminishing_flags,
            task.ai_call_counter,
            task.created_at,
            task.started_at,
            task.completed_at,
            task.error_message,
            task.output_pdf_path,
            task.output_docx_path,
            json.dumps(task.metadata),
            _meta.get("quality_tier", "balanced"),
            _meta.get("user_flow_instructions", ""),
            bool(_meta.get("continuous_mode", False)),
            bool(_meta.get("dont_kill_branches", False)),
        )
    logger.debug("Inserted ResearchTask id=%s", task.id)


async def get_research_task(pool: asyncpg.Pool, task_id: str) -> ResearchTask | None:
    """
    Retrieve a ResearchTask by its primary key.

    Returns:
        A :class:`ResearchTask` instance, or *None* if not found.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM research_tasks WHERE id = $1",
            task_id,
        )
    if row is None:
        return None
    data = _row_to_dict(row)
    data["status"] = TaskStatus(data["status"])
    data["current_state"] = State(data["current_state"])
    return ResearchTask.model_validate(data)


async def update_research_task(
    pool: asyncpg.Pool,
    task_id: str,
    **fields: Any,
) -> None:
    """
    Partially update a ResearchTask record.

    Only the keyword arguments supplied are updated.  Enum values are
    automatically serialised to their string representations.

    Args:
        pool:    Connection pool.
        task_id: Primary key of the task to update.
        **fields: Column-name → new-value pairs.
    """
    if not fields:
        return

    # Serialise enum values and dicts
    serialised: dict[str, Any] = {}
    for k, v in fields.items():
        if hasattr(v, "value"):  # Enum
            serialised[k] = v.value
        elif isinstance(v, dict):
            serialised[k] = json.dumps(v)
        else:
            serialised[k] = v

    # BUG-007: Use module-level constant; add assertion as defence-in-depth
    unknown = set(serialised.keys()) - _ALLOWED_TASK_COLUMNS
    if unknown:
        raise ValueError(f"update_research_task: unknown column(s): {unknown!r}")
    assert all(col in _ALLOWED_TASK_COLUMNS for col in serialised), "Allowlist bypass detected"

    set_clauses = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(serialised))
    values = list(serialised.values())

    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE research_tasks SET {set_clauses} WHERE id = $1",
            task_id,
            *values,
        )
    logger.debug("Updated ResearchTask id=%s fields=%s", task_id, list(serialised.keys()))


async def update_research_task_outputs(
    pool: asyncpg.Pool,
    task_id: str,
    output_pdf_path: str | None = None,
    output_docx_path: str | None = None,
) -> None:
    """Set the output file paths after report generation completes."""
    updates: dict[str, Any] = {}
    if output_pdf_path is not None:
        updates["output_pdf_path"] = output_pdf_path
    if output_docx_path is not None:
        updates["output_docx_path"] = output_docx_path
    if updates:
        await update_research_task(pool, task_id, **updates)


# ---------------------------------------------------------------------------
# Hypothesis CRUD
# ---------------------------------------------------------------------------


async def insert_hypothesis(pool: asyncpg.Pool, hypothesis: Hypothesis) -> None:
    """Insert a new Hypothesis record."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO hypotheses (
                id, task_id, parent_id, depth, statement, statement_zh,
                status, score, momentum_note, rationale, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11, $12
            )
            """,
            hypothesis.id,
            hypothesis.task_id,
            hypothesis.parent_id,
            hypothesis.depth,
            hypothesis.statement,
            hypothesis.statement_zh,
            hypothesis.status.value,
            hypothesis.score,
            hypothesis.momentum_note,
            hypothesis.rationale,
            hypothesis.created_at,
            hypothesis.updated_at,
        )
    logger.debug("Inserted Hypothesis id=%s task=%s", hypothesis.id, hypothesis.task_id)


async def get_hypotheses_for_task(
    pool: asyncpg.Pool,
    task_id: str,
) -> list[Hypothesis]:
    """Return all hypotheses belonging to a task, ordered by creation time."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM hypotheses WHERE task_id = $1 ORDER BY created_at ASC",
            task_id,
        )
    results: list[Hypothesis] = []
    for row in rows:
        data = _row_to_dict(row)
        # BUG-045: HypothesisStatus now imported at module level
        data["status"] = HypothesisStatus(data["status"])
        results.append(Hypothesis.model_validate(data))
    return results


# ---------------------------------------------------------------------------
# Finding CRUD
# ---------------------------------------------------------------------------


async def insert_finding(pool: asyncpg.Pool, finding: Finding) -> None:
    """Insert a new Finding record."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO findings (
                id, task_id, hypothesis_id, content, content_en, content_language,
                source_ids, confidence, evidence_type, is_compressed,
                raw_content_path, created_at, metadata
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10,
                $11, $12, $13
            )
            """,
            finding.id,
            finding.task_id,
            finding.hypothesis_id,
            finding.content,
            finding.content_en,
            finding.content_language,
            json.dumps(finding.source_ids),
            finding.confidence,
            finding.evidence_type.value,
            finding.is_compressed,
            finding.raw_content_path,
            finding.created_at,
            json.dumps(finding.metadata),
        )
    logger.debug("Inserted Finding id=%s hypothesis=%s", finding.id, finding.hypothesis_id)


async def get_findings_for_hypothesis(
    pool: asyncpg.Pool,
    hypothesis_id: str,
) -> list[Finding]:
    """Return all findings for a hypothesis, ordered by creation time."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM findings
            WHERE hypothesis_id = $1
            ORDER BY created_at ASC
            """,
            hypothesis_id,
        )
    results: list[Finding] = []
    for row in rows:
        data = _row_to_dict(row)
        # BUG-045: EvidenceType now imported at module level
        data["evidence_type"] = EvidenceType(data["evidence_type"])
        results.append(Finding.model_validate(data))
    return results


async def mark_finding_compressed(
    pool: asyncpg.Pool,
    finding_id: str,
    raw_content_path: str | None = None,
) -> None:
    """Mark a finding as compressed and optionally store the raw content path."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE findings
               SET is_compressed = TRUE,
                   raw_content_path = COALESCE($2, raw_content_path)
             WHERE id = $1
            """,
            finding_id,
            raw_content_path,
        )
    logger.debug("Marked Finding id=%s as compressed", finding_id)


# ---------------------------------------------------------------------------
# Source CRUD
# ---------------------------------------------------------------------------


async def insert_source(pool: asyncpg.Pool, source: Source) -> None:
    """
    Insert a new Source record.

    Silently ignores conflicts on ``url_hash`` (idempotent upsert semantics —
    the first fetch wins).
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sources (
                id, task_id, url, url_hash, title, title_en,
                content_hash, fetched_at, cache_expiry, source_type,
                language, adapter_name, is_paywalled, metadata
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10,
                $11, $12, $13, $14
            )
            ON CONFLICT (task_id, url_hash) DO NOTHING
            """,
            source.id,
            source.task_id,
            source.url,
            source.url_hash,
            source.title,
            source.title_en,
            source.content_hash,
            source.fetched_at,
            source.cache_expiry,
            source.source_type.value,
            source.language,
            source.adapter_name,
            source.is_paywalled,
            json.dumps(source.metadata),
        )
    logger.debug("Inserted Source url_hash=%s", source.url_hash)


async def get_source_by_url_hash(
    pool: asyncpg.Pool,
    url_hash: str,
    task_id: str,  # BUG-019: Required to avoid cross-task contamination
) -> Source | None:
    """Retrieve a Source by its URL hash within a specific task.

    The UNIQUE constraint is on (task_id, url_hash), not url_hash alone,
    so task_id must always be specified to avoid returning sources from
    other tasks that happen to share the same URL.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM sources WHERE url_hash = $1 AND task_id = $2",
            url_hash,
            task_id,
        )
    if row is None:
        return None
    data = _row_to_dict(row)
    data["source_type"] = SourceType(data["source_type"])
    return Source.model_validate(data)


# ---------------------------------------------------------------------------
# AISession CRUD
# ---------------------------------------------------------------------------


async def insert_ai_session(pool: asyncpg.Pool, session: AISession) -> None:
    """Insert an AI session record."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ai_sessions (
                id, task_id, branch_id, task_type, model_used,
                input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                cost_usd, duration_ms, used_batch_api, batch_id, cache_hit,
                started_at, error
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9,
                $10, $11, $12, $13, $14,
                $15, $16
            )
            """,
            session.id,
            session.task_id,
            session.branch_id,
            session.task_type.value,
            session.model_used.value,
            session.input_tokens,
            session.output_tokens,
            session.cache_creation_tokens,
            session.cache_read_tokens,
            session.cost_usd,
            session.duration_ms,
            session.used_batch_api,
            session.batch_id,
            session.cache_hit,
            session.started_at,
            session.error,
        )
    logger.debug(
        "Inserted AISession id=%s model=%s cost=$%.6f",
        session.id,
        session.model_used.value,
        session.cost_usd,
    )


# ---------------------------------------------------------------------------
# Branch CRUD
# ---------------------------------------------------------------------------


async def insert_branch(pool: asyncpg.Pool, branch: Branch) -> None:
    """Insert a new Branch record."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO branches (
                id, hypothesis_id, task_id, status,
                score_history, budget_allocated, budget_spent,
                grants_log, cycles_completed, kill_reason,
                sources_searched, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7,
                $8, $9, $10,
                $11, $12, $13
            )
            """,
            branch.id,
            branch.hypothesis_id,
            branch.task_id,
            branch.status.value,
            json.dumps(branch.score_history),
            branch.budget_allocated,
            branch.budget_spent,
            json.dumps(branch.grants_log),
            branch.cycles_completed,
            branch.kill_reason,
            json.dumps(branch.sources_searched),
            branch.created_at,
            branch.updated_at,
        )
    logger.debug("Inserted Branch id=%s hypothesis=%s", branch.id, branch.hypothesis_id)


async def get_branch(pool: asyncpg.Pool, branch_id: str) -> Branch | None:
    """Retrieve a Branch by primary key."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM branches WHERE id = $1",
            branch_id,
        )
    if row is None:
        return None
    data = _row_to_dict(row)
    data["status"] = BranchStatus(data["status"])
    return Branch.model_validate(data)


async def update_branch(
    pool: asyncpg.Pool,
    branch_id: str,
    **fields: Any,
) -> None:
    """
    Partially update a Branch record.

    List/dict fields (e.g. ``score_history``, ``grants_log``) are
    automatically JSON-serialised.
    """
    if not fields:
        return

    serialised: dict[str, Any] = {}
    for k, v in fields.items():
        if hasattr(v, "value"):
            serialised[k] = v.value
        elif isinstance(v, (dict, list)):
            serialised[k] = json.dumps(v)
        else:
            serialised[k] = v

    # BUG-007: Use module-level constant; add assertion as defence-in-depth
    unknown = set(serialised.keys()) - _ALLOWED_BRANCH_COLUMNS
    if unknown:
        raise ValueError(f"update_branch: unknown column(s): {unknown!r}")
    assert all(col in _ALLOWED_BRANCH_COLUMNS for col in serialised), "Allowlist bypass detected"

    set_clauses = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(serialised))
    values = list(serialised.values())

    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE branches SET {set_clauses} WHERE id = $1",
            branch_id,
            *values,
        )
    logger.debug("Updated Branch id=%s fields=%s", branch_id, list(serialised.keys()))


async def get_active_branches(
    pool: asyncpg.Pool,
    task_id: str,
) -> list[Branch]:
    """Return all ACTIVE branches for a task, ordered by creation time."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM branches
            WHERE task_id = $1 AND status = 'ACTIVE'
            ORDER BY created_at ASC
            """,
            task_id,
        )
    results: list[Branch] = []
    for row in rows:
        data = _row_to_dict(row)
        data["status"] = BranchStatus(data["status"])
        results.append(Branch.model_validate(data))
    return results


# ---------------------------------------------------------------------------
# Checkpoint CRUD
# ---------------------------------------------------------------------------


async def insert_checkpoint(pool: asyncpg.Pool, checkpoint: Checkpoint) -> None:
    """Insert a new Checkpoint snapshot record."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO checkpoints (
                id, task_id, timestamp, state_machine_state,
                active_branch_ids, killed_branch_ids, compressed_findings,
                budget_remaining, total_spent, diminishing_flags,
                ai_call_counter, snapshot_path, diminishing_result
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7,
                $8, $9, $10,
                $11, $12, $13
            )
            """,
            checkpoint.id,
            checkpoint.task_id,
            checkpoint.timestamp,
            checkpoint.state_machine_state.value,
            json.dumps(checkpoint.active_branch_ids),
            json.dumps(checkpoint.killed_branch_ids),
            json.dumps(checkpoint.compressed_findings),
            checkpoint.budget_remaining,
            checkpoint.total_spent,
            checkpoint.diminishing_flags,
            checkpoint.ai_call_counter,
            checkpoint.snapshot_path,
            checkpoint.diminishing_result.value if checkpoint.diminishing_result else None,
        )
    logger.debug(
        "Inserted Checkpoint id=%s task=%s state=%s",
        checkpoint.id,
        checkpoint.task_id,
        checkpoint.state_machine_state.value,
    )


# ---------------------------------------------------------------------------
# Checkpoint retrieval helpers
# ---------------------------------------------------------------------------


async def get_checkpoint(
    pool: asyncpg.Pool,
    checkpoint_id: str,
) -> Checkpoint | None:
    """Retrieve a specific Checkpoint by its primary key.

    Returns:
        A :class:`Checkpoint` instance, or *None* if not found.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, task_id, timestamp, state_machine_state,
                   active_branch_ids, killed_branch_ids, compressed_findings,
                   budget_remaining, total_spent, diminishing_flags,
                   ai_call_counter, snapshot_path, diminishing_result
            FROM checkpoints WHERE id = $1
            """,
            checkpoint_id,
        )
    if row is None:
        return None
    data = _row_to_dict(row)
    data["state_machine_state"] = State(data["state_machine_state"])
    return Checkpoint.model_validate(data)


async def get_latest_checkpoint(
    pool: asyncpg.Pool,
    task_id: str,
) -> Checkpoint | None:
    """Retrieve the most recent Checkpoint for a task.

    Returns:
        The most recent :class:`Checkpoint`, or *None* if none exist.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, task_id, timestamp, state_machine_state,
                   active_branch_ids, killed_branch_ids, compressed_findings,
                   budget_remaining, total_spent, diminishing_flags,
                   ai_call_counter, snapshot_path, diminishing_result
            FROM checkpoints
            WHERE task_id = $1
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            task_id,
        )
    if row is None:
        return None
    data = _row_to_dict(row)
    data["state_machine_state"] = State(data["state_machine_state"])
    logger.debug(
        "Retrieved latest Checkpoint task=%s checkpoint=%s",
        task_id,
        data["id"],
    )
    return Checkpoint.model_validate(data)


# ---------------------------------------------------------------------------
# TribunalSession CRUD
# ---------------------------------------------------------------------------


async def insert_tribunal_session(
    pool: asyncpg.Pool,
    session: TribunalSession,
) -> None:
    """Insert a new TribunalSession record."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tribunal_sessions (
                id, task_id, finding_id,
                plaintiff_args, defendant_args,
                plaintiff_rebuttal, defendant_counter,
                verdict, judge_plaintiff_score, judge_defendant_score,
                judge_reasoning, unanswered_questions,
                total_cost_usd, created_at
            ) VALUES (
                $1, $2, $3,
                $4, $5,
                $6, $7,
                $8, $9, $10,
                $11, $12,
                $13, $14
            )
            """,
            session.id,
            session.task_id,
            session.finding_id,
            session.plaintiff_args,
            session.defendant_args,
            session.plaintiff_rebuttal,
            session.defendant_counter,
            session.verdict.value if session.verdict else None,
            session.judge_plaintiff_score,
            session.judge_defendant_score,
            session.judge_reasoning,
            json.dumps(session.unanswered_questions),
            session.total_cost_usd,
            session.created_at,
        )
    logger.debug(
        "Inserted TribunalSession id=%s finding=%s verdict=%s",
        session.id,
        session.finding_id,
        session.verdict,
    )


# ---------------------------------------------------------------------------
# SkepticResult CRUD
# ---------------------------------------------------------------------------


async def insert_skeptic_result(
    pool: asyncpg.Pool,
    result: SkepticResult,
) -> None:
    """Insert a new SkepticResult record."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO skeptic_results (
                id, task_id, finding_id, tribunal_session_id,
                questions, open_count, researchable_count, resolved_count,
                critical_open_count, passes_publishing_threshold,
                cost_usd, created_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8,
                $9, $10,
                $11, $12
            )
            """,
            result.id,
            result.task_id,
            result.finding_id,
            result.tribunal_session_id,
            json.dumps([q.model_dump() for q in result.questions]),
            result.open_count,
            result.researchable_count,
            result.resolved_count,
            result.critical_open_count,
            result.passes_publishing_threshold,
            result.cost_usd,
            result.created_at,
        )
    logger.debug(
        "Inserted SkepticResult id=%s finding=%s open=%d critical=%d",
        result.id,
        result.finding_id,
        result.open_count,
        result.critical_open_count,
    )
