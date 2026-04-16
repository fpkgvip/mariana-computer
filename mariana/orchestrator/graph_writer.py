"""
mariana/orchestrator/graph_writer.py

Async helpers that the event loop calls to persist investigation graph data
(nodes and edges) as the AI discovers entities and relationships, and to
broadcast real-time ``graph_update`` SSE events via Redis pub/sub.

Design principles
-----------------
* **Fire-and-forget**: every public function logs errors but never raises.
  A graph-write failure must never abort an investigation.
* **Idempotent upserts**: all DB writes use ``ON CONFLICT (id) DO UPDATE``
  so functions can be called multiple times for the same entity safely.
* **Minimal coupling**: this module only imports from the standard library,
  ``asyncpg``, and ``mariana.data.models``.  It does *not* import api.py or
  anything that would create a circular dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import asyncpg

# BUG-D1-12 fix: module-level strong reference set prevents fire-and-forget async
# tasks from being garbage-collected before they complete.  The done callback
# removes each task from the set once finished, keeping memory bounded.
_background_tasks: set[Any] = set()

from mariana.data.models import Branch, Finding, Hypothesis, Source

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _new_id() -> str:
    """Generate a fresh UUID4 string."""
    return str(uuid.uuid4())


async def _upsert_node(
    db: asyncpg.Pool,
    *,
    node_id: str,
    task_id: str,
    label: str,
    node_type: str,
    description: str = "",
    metadata: dict[str, Any] | None = None,
    x: float | None = None,
    y: float | None = None,
    source: str = "ai",
) -> None:
    """Insert or update a single graph node in the DB.

    Args:
        db:          AsyncPG connection pool.
        node_id:     Stable unique identifier for this node (usually the
                     entity's own ``id``).
        task_id:     Parent research task.
        label:       Human-readable display label.
        node_type:   Semantic type tag (e.g. ``finding``, ``hypothesis``,
                     ``source``, ``branch``).
        description: Optional longer description.
        metadata:    Arbitrary JSON-serialisable metadata dict.
        x:           Optional canvas X coordinate.
        y:           Optional canvas Y coordinate.
        source:      Provenance tag (default ``"ai"``).
    """
    meta_json = json.dumps(metadata or {})
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO graph_nodes (
                id, task_id, label, type, description,
                metadata, x, y, source
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (id) DO UPDATE
                SET label       = EXCLUDED.label,
                    type        = EXCLUDED.type,
                    description = EXCLUDED.description,
                    metadata    = EXCLUDED.metadata,
                    x           = EXCLUDED.x,
                    y           = EXCLUDED.y,
                    source      = EXCLUDED.source
            """,
            node_id,
            task_id,
            label,
            node_type,
            description,
            meta_json,
            x,
            y,
            source,
        )


async def _upsert_edge(
    db: asyncpg.Pool,
    *,
    edge_id: str,
    task_id: str,
    source_node: str,
    target_node: str,
    label: str = "",
    metadata: dict[str, Any] | None = None,
    source: str = "ai",
) -> None:
    """Insert or update a single graph edge in the DB.

    Args:
        db:          AsyncPG connection pool.
        edge_id:     Stable unique identifier for this edge.
        task_id:     Parent research task.
        source_node: ID of the originating node.
        target_node: ID of the destination node.
        label:       Human-readable relationship label.
        metadata:    Arbitrary JSON-serialisable metadata dict.
        source:      Provenance tag (default ``"ai"``).
    """
    meta_json = json.dumps(metadata or {})
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO graph_edges (
                id, task_id, source_node, target_node,
                label, metadata, source
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id) DO UPDATE
                SET source_node = EXCLUDED.source_node,
                    target_node = EXCLUDED.target_node,
                    label       = EXCLUDED.label,
                    metadata    = EXCLUDED.metadata,
                    source      = EXCLUDED.source
            """,
            edge_id,
            task_id,
            source_node,
            target_node,
            label,
            meta_json,
            source,
        )


# ---------------------------------------------------------------------------
# Redis pub/sub helper
# ---------------------------------------------------------------------------


async def emit_graph_event(
    redis_client: Any,
    task_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Publish a ``graph_update`` SSE event to the ``logs:{task_id}`` channel.

    The frontend's SSE listener will pick up this event and update the live
    graph visualisation without a page reload.

    The event payload format:
    .. code-block:: json

        {
            "type": "graph_update",
            "nodes": [ ... ],
            "edges": [ ... ]
        }

    Args:
        redis_client: An ``aioredis.Redis`` async client (or ``None`` when
                      Redis is unavailable — the call becomes a no-op).
        task_id:      Research task whose SSE channel should receive the event.
        nodes:        List of node dicts to include in the event.
        edges:        List of edge dicts to include in the event.
    """
    if redis_client is None:
        return

    event: dict[str, Any] = {
        "type": "graph_update",
        "nodes": nodes,
        "edges": edges,
    }
    payload = json.dumps(event)

    try:
        loop = asyncio.get_running_loop()
        bg_task = loop.create_task(
            redis_client.publish(f"logs:{task_id}", payload)
        )
        # BUG-D1-12 fix: hold a strong reference in _background_tasks so the
        # task isn't garbage-collected before the coroutine completes.  The
        # lambda _t: None pattern does NOT prevent GC — only set membership does.
        _background_tasks.add(bg_task)
        bg_task.add_done_callback(_background_tasks.discard)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "graph_emit_failed task_id=%s error=%s", task_id, exc
        )


def _node_dict(
    node_id: str,
    label: str,
    node_type: str,
    description: str = "",
    metadata: dict[str, Any] | None = None,
    x: float | None = None,
    y: float | None = None,
    source: str = "ai",
) -> dict[str, Any]:
    """Build a plain node dict suitable for Redis SSE events."""
    return {
        "id": node_id,
        "label": label,
        "type": node_type,
        "description": description,
        "metadata": metadata or {},
        "x": x,
        "y": y,
        "source": source,
    }


def _edge_dict(
    edge_id: str,
    source_node: str,
    target_node: str,
    label: str = "",
    metadata: dict[str, Any] | None = None,
    source: str = "ai",
) -> dict[str, Any]:
    """Build a plain edge dict suitable for Redis SSE events."""
    return {
        "id": edge_id,
        "source": source_node,   # D3 convention
        "target": target_node,   # D3 convention
        "label": label,
        "metadata": metadata or {},
        "source_origin": source,
    }


# ---------------------------------------------------------------------------
# Public node-writer functions
# ---------------------------------------------------------------------------


async def add_finding_node(
    db: asyncpg.Pool,
    task_id: str,
    finding: Finding,
    redis_client: Any = None,
) -> None:
    """Persist a Finding as a knowledge-graph node and emit a Redis event.

    The node ID mirrors the Finding's own ``id`` for easy cross-referencing.
    The label is derived from the first 120 characters of the finding content
    to give a meaningful but compact display string.

    Args:
        db:           AsyncPG pool.
        task_id:      Parent task ID.
        finding:      The Finding entity to represent.
        redis_client: Optional Redis client for SSE broadcast.
    """
    label = (finding.content[:117] + "...") if len(finding.content) > 120 else finding.content
    meta: dict[str, Any] = {
        "hypothesis_id": finding.hypothesis_id,
        "evidence_type": finding.evidence_type.value,
        "confidence": finding.confidence,
        "content_language": finding.content_language,
    }

    try:
        await _upsert_node(
            db,
            node_id=finding.id,
            task_id=task_id,
            label=label,
            node_type="finding",
            description=finding.content_en or finding.content,
            metadata=meta,
            source="ai",
        )
        node = _node_dict(
            finding.id, label, "finding",
            description=finding.content_en or finding.content,
            metadata=meta,
        )
        await emit_graph_event(redis_client, task_id, [node], [])
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "add_finding_node_failed task_id=%s finding_id=%s error=%s",
            task_id, finding.id, exc,
        )


async def add_hypothesis_node(
    db: asyncpg.Pool,
    task_id: str,
    hypothesis: Hypothesis,
    redis_client: Any = None,
) -> None:
    """Persist a Hypothesis as a knowledge-graph node and emit a Redis event.

    Args:
        db:           AsyncPG pool.
        task_id:      Parent task ID.
        hypothesis:   The Hypothesis entity to represent.
        redis_client: Optional Redis client for SSE broadcast.
    """
    label = hypothesis.statement[:120]
    meta: dict[str, Any] = {
        "status": hypothesis.status.value,
        "depth": hypothesis.depth,
        "score": hypothesis.score,
    }
    if hypothesis.parent_id:
        meta["parent_id"] = hypothesis.parent_id

    try:
        await _upsert_node(
            db,
            node_id=hypothesis.id,
            task_id=task_id,
            label=label,
            node_type="hypothesis",
            description=hypothesis.rationale or "",
            metadata=meta,
            source="ai",
        )
        node = _node_dict(
            hypothesis.id, label, "hypothesis",
            description=hypothesis.rationale or "",
            metadata=meta,
        )
        await emit_graph_event(redis_client, task_id, [node], [])
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "add_hypothesis_node_failed task_id=%s hypothesis_id=%s error=%s",
            task_id, hypothesis.id, exc,
        )


async def add_source_node(
    db: asyncpg.Pool,
    task_id: str,
    source: Source,
    redis_client: Any = None,
) -> None:
    """Persist a Source (URL) as a knowledge-graph node and emit a Redis event.

    Args:
        db:           AsyncPG pool.
        task_id:      Parent task ID.
        source:       The Source entity to represent.
        redis_client: Optional Redis client for SSE broadcast.
    """
    label = source.title or source.url[:80]
    meta: dict[str, Any] = {
        "url": source.url,
        "source_type": source.source_type.value,
        "language": source.language,
        "is_paywalled": source.is_paywalled,
    }

    try:
        await _upsert_node(
            db,
            node_id=source.id,
            task_id=task_id,
            label=label,
            node_type="source",
            description=source.title_en or source.title or source.url,
            metadata=meta,
            source="ai",
        )
        node = _node_dict(
            source.id, label, "source",
            description=source.title_en or source.title or source.url,
            metadata=meta,
        )
        await emit_graph_event(redis_client, task_id, [node], [])
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "add_source_node_failed task_id=%s source_id=%s error=%s",
            task_id, source.id, exc,
        )


async def add_branch_node(
    db: asyncpg.Pool,
    task_id: str,
    branch: Branch,
    redis_client: Any = None,
) -> None:
    """Persist a Branch as a knowledge-graph node and emit a Redis event.

    Args:
        db:           AsyncPG pool.
        task_id:      Parent task ID.
        branch:       The Branch entity to represent.
        redis_client: Optional Redis client for SSE broadcast.
    """
    label = f"Branch {branch.id[:8]}"
    meta: dict[str, Any] = {
        "hypothesis_id": branch.hypothesis_id,
        "status": branch.status.value,
        "cycles_completed": branch.cycles_completed,
        "budget_allocated": branch.budget_allocated,
        "budget_spent": branch.budget_spent,
    }

    try:
        await _upsert_node(
            db,
            node_id=branch.id,
            task_id=task_id,
            label=label,
            node_type="branch",
            description=branch.kill_reason or "",
            metadata=meta,
            source="ai",
        )
        node = _node_dict(
            branch.id, label, "branch",
            description=branch.kill_reason or "",
            metadata=meta,
        )
        await emit_graph_event(redis_client, task_id, [node], [])
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "add_branch_node_failed task_id=%s branch_id=%s error=%s",
            task_id, branch.id, exc,
        )


# ---------------------------------------------------------------------------
# Public edge-writer functions
# ---------------------------------------------------------------------------


async def add_evidence_edge(
    db: asyncpg.Pool,
    task_id: str,
    finding_id: str,
    hypothesis_id: str,
    evidence_type: str,
    redis_client: Any = None,
) -> None:
    """Link a Finding node to a Hypothesis node with an evidence relationship.

    The edge ID is deterministically derived from the (finding, hypothesis)
    pair so repeated calls are idempotent.

    Args:
        db:             AsyncPG pool.
        task_id:        Parent task ID.
        finding_id:     ID of the Finding node.
        hypothesis_id:  ID of the Hypothesis node.
        evidence_type:  One of ``"FOR"``, ``"AGAINST"``, or ``"NEUTRAL"``.
        redis_client:   Optional Redis client for SSE broadcast.
    """
    # Deterministic edge ID — same pair always produces the same ID.
    edge_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"evidence:{finding_id}:{hypothesis_id}"))
    label = evidence_type.upper()
    meta: dict[str, Any] = {"evidence_type": evidence_type}

    try:
        await _upsert_edge(
            db,
            edge_id=edge_id,
            task_id=task_id,
            source_node=finding_id,
            target_node=hypothesis_id,
            label=label,
            metadata=meta,
            source="ai",
        )
        edge = _edge_dict(edge_id, finding_id, hypothesis_id, label=label, metadata=meta)
        await emit_graph_event(redis_client, task_id, [], [edge])
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "add_evidence_edge_failed task_id=%s finding_id=%s "
            "hypothesis_id=%s error=%s",
            task_id, finding_id, hypothesis_id, exc,
        )


async def add_source_edge(
    db: asyncpg.Pool,
    task_id: str,
    finding_id: str,
    source_id: str,
    redis_client: Any = None,
) -> None:
    """Link a Finding node to the Source node it was extracted from.

    The edge ID is deterministically derived from the (finding, source) pair.

    Args:
        db:           AsyncPG pool.
        task_id:      Parent task ID.
        finding_id:   ID of the Finding node.
        source_id:    ID of the Source node.
        redis_client: Optional Redis client for SSE broadcast.
    """
    edge_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"sourced_from:{finding_id}:{source_id}"))
    label = "sourced_from"
    meta: dict[str, Any] = {}

    try:
        await _upsert_edge(
            db,
            edge_id=edge_id,
            task_id=task_id,
            source_node=finding_id,
            target_node=source_id,
            label=label,
            metadata=meta,
            source="ai",
        )
        edge = _edge_dict(edge_id, finding_id, source_id, label=label)
        await emit_graph_event(redis_client, task_id, [], [edge])
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "add_source_edge_failed task_id=%s finding_id=%s source_id=%s error=%s",
            task_id, finding_id, source_id, exc,
        )
