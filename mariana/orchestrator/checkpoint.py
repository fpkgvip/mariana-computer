"""
mariana/orchestrator/checkpoint.py

Checkpoint save / restore for crash recovery.

A checkpoint captures the full orchestrator state at a given moment:
  - Task metadata (state-machine state, budget, flags)
  - Active and killed branch lists
  - All current findings (finding IDs only — full content stays in DB)
  - Cost-tracker snapshot
  - A full JSON blob written to disk under ``{data_root}/checkpoints/``

On restart the event loop calls :func:`load_latest_checkpoint` to find the
most recent snapshot and resume from there.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from mariana.data.db import _row_to_dict
from mariana.data.models import (
    Branch,
    Checkpoint,
    DiminishingRecommendation,
    Finding,
    ResearchTask,
    State,
)
from mariana.orchestrator.cost_tracker import CostTracker

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _findings_summary(findings: list[Finding]) -> list[dict[str, Any]]:
    """Produce a lightweight serialisable summary of findings.

    For the prototype we serialise the key identifying fields rather than
    calling an AI compression model (which would be expensive and introduce
    an async call into a save path that needs to be fast).

    Each entry in the returned list is a plain dict with:
      - ``id``, ``hypothesis_id``, ``evidence_type``, ``confidence``,
        ``is_compressed``, ``source_ids``
    """
    return [
        {
            "id": f.id,
            "hypothesis_id": f.hypothesis_id,
            "evidence_type": f.evidence_type.value,
            "confidence": f.confidence,
            "is_compressed": f.is_compressed,
            "source_ids": f.source_ids,
        }
        for f in findings
    ]


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


async def save_checkpoint(
    task: ResearchTask,
    active_branches: list[Branch],
    killed_branches: list[Branch],
    findings: list[Finding],
    current_state: State,
    cost_tracker: CostTracker,
    db: Any,  # asyncpg.Pool
    data_root: str,
    diminishing_result: DiminishingRecommendation | None = None,  # BUG-R3-05
) -> str:
    """Serialise and persist the current orchestrator state.

    Steps
    -----
    1. Build a compressed findings summary (lightweight dict list — no AI call).
    2. Construct a :class:`~mariana.data.models.Checkpoint` Pydantic model.
    3. Write a full JSON snapshot to
       ``{data_root}/checkpoints/{task_id}_{timestamp}.json``.
    4. Upsert the checkpoint record into the ``checkpoints`` DB table.
    5. Return the checkpoint UUID.

    Parameters
    ----------
    task:
        The current ResearchTask (budget, flags, etc.).
    active_branches:
        Branches still being explored.
    killed_branches:
        Branches that have been terminated (for provenance).
    findings:
        All Finding records accumulated so far.
    current_state:
        The state-machine state at the moment of the checkpoint.
    cost_tracker:
        Live cost tracker whose snapshot is embedded in the JSON blob.
    db:
        asyncpg connection pool.
    data_root:
        Filesystem root for persistent data (e.g. ``/data``).

    Returns
    -------
    str
        The UUID of the newly created checkpoint.
    """
    checkpoint_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)  # BUG-001
    timestamp_str = now.strftime("%Y%m%dT%H%M%S")

    # ------------------------------------------------------------------ #
    # Step 1 – Build findings summary
    # ------------------------------------------------------------------ #
    compressed_finding_ids = [f.id for f in findings if f.is_compressed]
    findings_summary = _findings_summary(findings)

    # ------------------------------------------------------------------ #
    # Step 2 – Build Checkpoint model
    # ------------------------------------------------------------------ #
    checkpoint = Checkpoint(
        id=checkpoint_id,
        task_id=task.id,
        timestamp=now,
        state_machine_state=current_state,
        active_branch_ids=[b.id for b in active_branches],
        killed_branch_ids=[b.id for b in killed_branches],
        compressed_findings=compressed_finding_ids,
        budget_remaining=cost_tracker.budget_remaining,
        total_spent=cost_tracker.total_spent,
        diminishing_flags=task.diminishing_flags,
        ai_call_counter=task.ai_call_counter,
        diminishing_result=diminishing_result,  # BUG-R3-05
    )

    # ------------------------------------------------------------------ #
    # Step 3 – Determine snapshot path and write to a temp file first
    # ------------------------------------------------------------------ #
    checkpoints_dir = Path(data_root) / "checkpoints"
    checkpoints_dir.mkdir(parents=True, exist_ok=True)

    snapshot_filename = f"{task.id}_{timestamp_str}.json"
    snapshot_path = checkpoints_dir / snapshot_filename
    # Write to a .tmp file first; rename after DB insert succeeds (atomic)
    tmp_path = checkpoints_dir / f"{snapshot_filename}.tmp"

    snapshot_blob: dict[str, Any] = {
        "checkpoint_id": checkpoint_id,
        "task_id": task.id,
        "timestamp": now.isoformat(),
        "state_machine_state": current_state.value,
        "task": task.model_dump(mode="json"),
        "active_branch_ids": checkpoint.active_branch_ids,
        "killed_branch_ids": checkpoint.killed_branch_ids,
        "active_branches": [b.model_dump(mode="json") for b in active_branches],
        "killed_branches": [b.model_dump(mode="json") for b in killed_branches],
        "findings_summary": findings_summary,
        "compressed_finding_ids": compressed_finding_ids,
        "cost_tracker": cost_tracker.to_model().model_dump(mode="json"),
        "diminishing_flags": task.diminishing_flags,
        "ai_call_counter": task.ai_call_counter,
        "budget_remaining": cost_tracker.budget_remaining,
        "total_spent": cost_tracker.total_spent,
    }

    try:
        tmp_path.write_text(
            json.dumps(snapshot_blob, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error(
            "checkpoint_disk_write_failed",
            path=str(tmp_path),
            error=str(exc),
        )
        raise

    checkpoint.snapshot_path = str(snapshot_path)

    # ------------------------------------------------------------------ #
    # Step 4 – Upsert into DB; only rename temp file if DB insert succeeds
    # ------------------------------------------------------------------ #
    try:
        await db.execute(
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
            ON CONFLICT (id) DO UPDATE SET
                snapshot_path = EXCLUDED.snapshot_path,
                timestamp = EXCLUDED.timestamp
            """,
            checkpoint.id,
            checkpoint.task_id,
            checkpoint.timestamp,
            checkpoint.state_machine_state.value,
            # BUG-008: json.dumps() required for JSONB columns in asyncpg
            json.dumps(checkpoint.active_branch_ids),
            json.dumps(checkpoint.killed_branch_ids),
            json.dumps(checkpoint.compressed_findings),
            checkpoint.budget_remaining,
            checkpoint.total_spent,
            checkpoint.diminishing_flags,
            checkpoint.ai_call_counter,
            checkpoint.snapshot_path,
            # BUG-R3-05: persist diminishing_result so crash recovery has it
            checkpoint.diminishing_result.value if checkpoint.diminishing_result else None,
        )
    except Exception:
        # DB insert failed — remove the orphaned temp file and re-raise
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    # DB insert succeeded — atomically rename temp file to final path
    try:
        tmp_path.rename(snapshot_path)
    except OSError as exc:
        logger.error(
            "checkpoint_rename_failed",
            tmp_path=str(tmp_path),
            final_path=str(snapshot_path),
            error=str(exc),
        )
        # BUG-038: Clean up orphaned temp file and clear invalid snapshot_path
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            await db.execute(
                "UPDATE checkpoints SET snapshot_path = NULL WHERE id = $1",
                checkpoint_id,
            )
        except Exception:  # noqa: BLE001
            pass
        raise

    logger.info(
        "checkpoint_saved",
        checkpoint_id=checkpoint_id,
        task_id=task.id,
        state=current_state.value,
        snapshot_path=str(snapshot_path),
        active_branches=len(active_branches),
        killed_branches=len(killed_branches),
        findings=len(findings),
        total_spent=cost_tracker.total_spent,
    )

    return checkpoint_id


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


async def load_latest_checkpoint(
    task_id: str,
    db: Any,  # asyncpg.Pool
    data_root: str,
) -> Checkpoint | None:
    """Load the most recent checkpoint for a task, if one exists.

    Queries the database for the newest checkpoint by ``timestamp DESC``
    and returns the deserialised :class:`~mariana.data.models.Checkpoint`
    model.  The caller is responsible for reading the full JSON snapshot
    from ``checkpoint.snapshot_path`` if it needs the complete state blob.

    Parameters
    ----------
    task_id:
        UUID of the ResearchTask to recover.
    db:
        asyncpg connection pool.
    data_root:
        Filesystem root (used to verify the snapshot file exists on disk).

    Returns
    -------
    Checkpoint or None
        The most recent checkpoint, or ``None`` if no checkpoint exists.
    """
    row = await db.fetchrow(
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
        logger.info("no_checkpoint_found", task_id=task_id)
        return None

    # BUG-022: Decode JSONB columns and convert enum before model_validate
    data = _row_to_dict(row)
    # Decode JSONB list fields if they came back as strings
    for _col in ("active_branch_ids", "killed_branch_ids", "compressed_findings"):
        if isinstance(data.get(_col), str):
            data[_col] = json.loads(data[_col])
    data["state_machine_state"] = State(data["state_machine_state"])
    checkpoint = Checkpoint.model_validate(data)

    # Warn if the snapshot file is missing (DB record exists but disk file
    # was deleted — e.g. container volume was remounted).
    if checkpoint.snapshot_path:
        snapshot_path = Path(checkpoint.snapshot_path)
        if not snapshot_path.exists():
            logger.warning(
                "checkpoint_snapshot_missing",
                checkpoint_id=checkpoint.id,
                snapshot_path=checkpoint.snapshot_path,
            )

    logger.info(
        "checkpoint_loaded",
        checkpoint_id=checkpoint.id,
        task_id=task_id,
        state=checkpoint.state_machine_state.value,
        timestamp=checkpoint.timestamp.isoformat(),
        total_spent=checkpoint.total_spent,
    )

    return checkpoint


async def load_checkpoint_blob(snapshot_path: str) -> dict[str, Any]:
    """Read and parse the full JSON snapshot from disk.

    Parameters
    ----------
    snapshot_path:
        Absolute filesystem path to the ``.json`` snapshot file.

    Returns
    -------
    dict
        The full deserialised snapshot dictionary.

    Raises
    ------
    FileNotFoundError
        If the snapshot file does not exist on disk.
    json.JSONDecodeError
        If the snapshot file is corrupt / not valid JSON.
    """
    path = Path(snapshot_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Checkpoint snapshot not found: {snapshot_path!r}"
        )

    raw = path.read_text(encoding="utf-8")
    blob: dict[str, Any] = json.loads(raw)

    logger.debug(
        "checkpoint_blob_loaded",
        snapshot_path=snapshot_path,
        task_id=blob.get("task_id"),
        state=blob.get("state_machine_state"),
    )
    return blob
