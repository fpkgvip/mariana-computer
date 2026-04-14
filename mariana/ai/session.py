"""
mariana/ai/session.py

Core AI session management for the Mariana system.

``spawn_model()`` is the single entry point for every AI call in the system.
It orchestrates:

  1. Model routing via ``router.get_model_config()``
  2. Prompt construction via ``prompt_builder.build_messages()``
  3. HTTP call to the LLM Gateway (OpenAI-compatible endpoint)
  4. Retry logic: 3× with exponential backoff on transient failures
  5. Output parsing via ``output_parser.parse_output()``
  6. Retry once on parse failure with an error-hint injected
  7. Token-based cost computation
  8. AISession record construction
  9. Optional persistence to PostgreSQL
  10. Optional cost tracking

All providers (Claude, DeepSeek, GPT-4o-mini) are called through a single
LLM Gateway endpoint configured by ``config.LLM_GATEWAY_BASE_URL`` and
``config.LLM_GATEWAY_API_KEY``.  The gateway exposes an OpenAI-compatible
``POST /chat/completions`` API, so the same request format works for all
models.

Error handling
--------------
``ModelCallError``       — The API was unreachable or returned a non-transient
                           error after exhausting retries.
``OutputParseError``     — The model response could not be parsed as the
                           expected Pydantic schema, even after the one-shot
                           repair retry.
``BudgetExhaustedError`` — The cost_tracker reports that the budget cap has
                           been exceeded.
``ContextTooLargeError`` — The assembled prompt exceeds the configured context
                           token limit.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel

from mariana.data.models import AISession, ModelID, TaskType
from mariana.config import AppConfig, load_config
from mariana.ai.router import ModelConfig, get_model_config
from mariana.ai.prompt_builder import build_messages
from mariana.ai.output_parser import OutputParseError, build_error_hint, parse_output

logger = logging.getLogger(__name__)

# ─── Exception types ─────────────────────────────────────────────────────────


class ModelCallError(Exception):
    """
    Raised when the LLM Gateway returns an unrecoverable error or is
    unreachable after all retry attempts.

    Attributes:
        status_code: HTTP status code of the last failure, or None for
            network-level errors.
        response_body: Raw response text for debugging, truncated to 500 chars.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body[:500] if response_body else ""


class BudgetExhaustedError(Exception):
    """
    Raised when the cost_tracker's hard cap has been exceeded before or
    during an AI call.

    Attributes:
        cap_usd: The budget cap that was exceeded.
        spent_usd: The amount spent so far.
    """

    def __init__(self, cap_usd: float, spent_usd: float) -> None:
        super().__init__(
            f"Budget cap exceeded: spent=${spent_usd:.4f} cap=${cap_usd:.4f}"
        )
        self.cap_usd = cap_usd
        self.spent_usd = spent_usd


class ContextTooLargeError(Exception):
    """
    Raised when the assembled prompt exceeds the configured maximum context
    token limit.

    Attributes:
        estimated_tokens: Approximate token count of the rejected prompt.
        max_tokens: The configured ``AI_SESSION_MAX_TOKENS_CONTEXT`` limit.
    """

    def __init__(self, estimated_tokens: int, max_tokens: int) -> None:
        super().__init__(
            f"Context too large: ~{estimated_tokens} tokens > max {max_tokens} tokens"
        )
        self.estimated_tokens = estimated_tokens
        self.max_tokens = max_tokens


# ─── Pricing table ───────────────────────────────────────────────────────────

# Per-million-token pricing in USD: (input_per_mtok, output_per_mtok)
# Cache write/read rates for Claude models:
#   write = 1.25× input rate, read = 0.10× input rate
# DeepSeek uses a binary hit/miss rate (cache_hit vs cache_miss for input).

_MODEL_PRICING: dict[ModelID, dict[str, float]] = {
    ModelID.OPUS_46: {
        "input_per_mtok": 15.00,
        "output_per_mtok": 75.00,
        "cache_write_per_mtok": 18.75,  # 1.25 × input
        "cache_read_per_mtok": 1.50,    # 0.10 × input
    },
    ModelID.SONNET_46: {
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_write_per_mtok": 3.75,
        "cache_read_per_mtok": 0.30,
    },
    ModelID.HAIKU_45: {
        "input_per_mtok": 0.80,
        "output_per_mtok": 4.00,
        "cache_write_per_mtok": 1.00,
        "cache_read_per_mtok": 0.08,
    },
    ModelID.DEEPSEEK_CHAT: {
        # DeepSeek has cache-hit / cache-miss input pricing.
        "input_per_mtok": 0.27,        # cache miss (non-cached input tokens)
        "output_per_mtok": 1.10,
        "cache_read_per_mtok": 0.07,   # cache hit
        "cache_write_per_mtok": 0.0,   # no explicit cache write cost
    },
    ModelID.DEEPSEEK_REASONER: {
        "input_per_mtok": 0.55,
        "output_per_mtok": 2.19,
        "cache_read_per_mtok": 0.14,
        "cache_write_per_mtok": 0.0,
    },
    ModelID.GPT4O_MINI: {
        "input_per_mtok": 0.15,
        "output_per_mtok": 0.60,
        "cache_write_per_mtok": 0.0,
        "cache_read_per_mtok": 0.075,  # OpenAI automatic prompt cache
    },
}

# ─── Retry configuration ─────────────────────────────────────────────────────

_RETRY_MAX_ATTEMPTS: int = 3
_RETRY_BACKOFF_SECONDS: list[float] = [2.0, 4.0, 8.0]

# HTTP status codes that trigger a retry (transient errors).
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# ─── Token estimation ─────────────────────────────────────────────────────────


def _estimate_tokens(text: str, model_id: ModelID) -> int:
    """
    Approximate token count for *text*.

    Uses tiktoken for OpenAI models when available; falls back to the
    characters-÷-3.5 heuristic for Claude and DeepSeek (close enough for
    budget / context checks, not for billing — actual counts come from the
    API response).
    """
    if model_id == ModelID.GPT4O_MINI:
        try:
            import tiktoken  # optional dependency
            enc = tiktoken.encoding_for_model("gpt-4o-mini")
            return len(enc.encode(text))
        except Exception:
            pass  # fall through to heuristic
    return max(1, int(len(text) / 3.5))


def _estimate_messages_tokens(messages: list[dict[str, Any]], model_id: ModelID) -> int:
    """Sum estimated tokens for all message content blocks."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(content, model_id)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += _estimate_tokens(block.get("text", ""), model_id)
    return total


# ─── Cost computation ─────────────────────────────────────────────────────────


def _compute_cost(
    model_id: ModelID,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """
    Compute the USD cost for a completed API call.

    For Claude models:
      cost = (input_tokens − cache_read_tokens) × input_rate
             + output_tokens × output_rate
             + cache_creation_tokens × cache_write_rate
             + cache_read_tokens × cache_read_rate

    For DeepSeek:
      cost = (input_tokens − cache_read_tokens) × input_rate (cache miss)
             + cache_read_tokens × cache_read_rate (cache hit)
             + output_tokens × output_rate

    For GPT-4o-mini:
      cost = (input_tokens − cache_read_tokens) × input_rate
             + cache_read_tokens × cache_read_rate
             + output_tokens × output_rate

    All rates are per-million-token.
    """
    pricing = _MODEL_PRICING.get(model_id)
    if pricing is None:
        logger.warning("No pricing data for model=%s — cost will be $0.0", model_id.value)
        return 0.0

    input_rate = pricing["input_per_mtok"] / 1_000_000
    output_rate = pricing["output_per_mtok"] / 1_000_000
    cache_write_rate = pricing.get("cache_write_per_mtok", 0.0) / 1_000_000
    cache_read_rate = pricing.get("cache_read_per_mtok", 0.0) / 1_000_000

    # Non-cached input tokens billed at normal input rate.
    normal_input_tokens = max(0, input_tokens - cache_read_tokens)

    cost = (
        normal_input_tokens * input_rate
        + output_tokens * output_rate
        + cache_creation_tokens * cache_write_rate
        + cache_read_tokens * cache_read_rate
    )
    return round(cost, 8)


# ─── LLM Gateway HTTP client ──────────────────────────────────────────────────


def _build_request_body(
    messages: list[dict[str, Any]],
    model_config: ModelConfig,
    max_tokens: int,
) -> dict[str, Any]:
    """
    Build the OpenAI-compatible request body for the LLM Gateway.

    We always include ``response_format={"type": "json_object"}`` — this
    instructs supporting models to return valid JSON.  The gateway should
    pass this through for models that support it and silently strip it for
    models that do not.
    """
    body: dict[str, Any] = {
        "model": model_config.model_id.value,
        "messages": messages,
        "temperature": model_config.temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    return body


def _extract_response_content(response_json: dict[str, Any]) -> str:
    """
    Extract the assistant message content from an OpenAI-compatible response.

    Raises:
        ModelCallError: If the expected structure is absent.
    """
    try:
        choices = response_json["choices"]
        if not choices:
            raise KeyError("empty choices list")
        content = choices[0]["message"]["content"]
        if content is None:
            raise KeyError("null content")
        return content
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelCallError(
            f"Unexpected LLM Gateway response structure: {exc}",
            response_body=str(response_json)[:500],
        ) from exc


def _extract_usage(response_json: dict[str, Any]) -> dict[str, int]:
    """
    Extract token usage from an OpenAI-compatible response.

    Returns a dict with keys: ``prompt_tokens``, ``completion_tokens``,
    ``cache_creation_tokens``, ``cache_read_tokens``.
    All values default to 0 if not present.
    """
    usage = response_json.get("usage", {})
    # Anthropic routes the cache token counts under these keys:
    cache_creation = (
        usage.get("cache_creation_input_tokens")
        or usage.get("cache_creation_tokens")
        or 0
    )
    cache_read = (
        usage.get("cache_read_input_tokens")
        or usage.get("cache_read_tokens")
        or 0
    )
    return {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "cache_creation_tokens": int(cache_creation),
        "cache_read_tokens": int(cache_read),
    }


async def _call_gateway(
    messages: list[dict[str, Any]],
    model_config: ModelConfig,
    max_tokens: int,
    config: AppConfig,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """
    Send a single HTTP request to the LLM Gateway.

    Returns the parsed JSON response dict.

    Raises:
        ModelCallError: On HTTP errors or network failures.
    """
    base_url = getattr(config, "LLM_GATEWAY_BASE_URL", "").rstrip("/")
    api_key = getattr(config, "LLM_GATEWAY_API_KEY", "")

    if not base_url:
        raise ModelCallError("LLM_GATEWAY_BASE_URL is not configured.")

    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = _build_request_body(messages, model_config, max_tokens)

    # BUG-006 fix: keep all response inspection INSIDE the async-with block so
    # the connection is still live if future refactors need to stream the body.
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(url, json=body, headers=headers)

        if response.status_code in _RETRYABLE_STATUS_CODES:
            raise ModelCallError(
                f"LLM Gateway transient error: HTTP {response.status_code}",
                status_code=response.status_code,
                response_body=response.text,
            )
        if response.status_code >= 400:
            raise ModelCallError(
                f"LLM Gateway non-retryable error: HTTP {response.status_code}",
                status_code=response.status_code,
                response_body=response.text,
            )

        # BUG-007 fix: catch only json.JSONDecodeError (a ValueError subclass),
        # not bare Exception which would swallow programming errors.
        try:
            return response.json()
        except ValueError as exc:
            raise ModelCallError(
                f"LLM Gateway returned non-JSON response: {exc}",
                status_code=response.status_code,
                response_body=response.text[:500],
            ) from exc


async def _call_gateway_with_retry(
    messages: list[dict[str, Any]],
    model_config: ModelConfig,
    max_tokens: int,
    config: AppConfig,
) -> dict[str, Any]:
    """
    Wrap ``_call_gateway`` with exponential-backoff retry logic.

    Retries on:
    - ``ModelCallError`` with a retryable status code (429, 5xx)
    - ``httpx.TimeoutException``
    - ``httpx.ConnectError``
    - ``httpx.RemoteProtocolError``

    Non-retryable ``ModelCallError`` (4xx except 429) is re-raised immediately.

    Raises:
        ModelCallError: After all retry attempts are exhausted.
    """
    last_error: Exception | None = None

    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            return await _call_gateway(messages, model_config, max_tokens, config)
        except ModelCallError as exc:
            # Don't retry non-transient client errors (e.g. 401, 403, 422).
            if exc.status_code is not None and exc.status_code not in _RETRYABLE_STATUS_CODES:
                logger.error(
                    "Non-retryable LLM Gateway error (HTTP %s): %s",
                    exc.status_code,
                    exc,
                )
                raise
            last_error = exc
            logger.warning(
                "LLM Gateway error (attempt %d/%d): %s",
                attempt + 1,
                _RETRY_MAX_ATTEMPTS,
                exc,
            )
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
        ) as exc:
            last_error = exc
            logger.warning(
                "LLM Gateway network error (attempt %d/%d): %s",
                attempt + 1,
                _RETRY_MAX_ATTEMPTS,
                exc,
            )

        if attempt < _RETRY_MAX_ATTEMPTS - 1:
            backoff = _RETRY_BACKOFF_SECONDS[attempt]
            logger.info("Retrying in %.1fs…", backoff)
            await asyncio.sleep(backoff)

    raise ModelCallError(
        f"LLM Gateway unreachable after {_RETRY_MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error


# ─── Database persistence ─────────────────────────────────────────────────────


async def _persist_session(db: Any, session: AISession) -> None:
    """
    Insert an ``AISession`` record into PostgreSQL.

    Uses the asyncpg pool directly with a parameterised query.
    Errors are logged but not re-raised — a persistence failure should not
    abort the research pipeline.
    """
    sql = """
        INSERT INTO ai_sessions (
            id, task_id, branch_id, task_type, model_used,
            input_tokens, output_tokens,
            cache_creation_tokens, cache_read_tokens,
            cost_usd, duration_ms, used_batch_api, batch_id,
            cache_hit, started_at, error
        ) VALUES (
            $1, $2, $3, $4, $5,
            $6, $7,
            $8, $9,
            $10, $11, $12, $13,
            $14, $15, $16
        )
        ON CONFLICT (id) DO NOTHING
    """
    try:
        await db.execute(
            sql,
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
        logger.debug("Persisted AISession id=%s", session.id)
    except Exception as exc:
        logger.error("Failed to persist AISession id=%s: %s", session.id, exc)


# ─── Cost tracker integration ─────────────────────────────────────────────────


def _record_cost(cost_tracker: Any, session: AISession, branch_id: str | None) -> None:
    """
    Record cost against the cost_tracker.

    Calls ``cost_tracker.record_call(session, branch_id)`` if the method
    exists.  Errors are logged but not re-raised.
    """
    try:
        record_fn = getattr(cost_tracker, "record_call", None)
        if callable(record_fn):
            record_fn(session, branch_id)
        else:
            logger.warning("cost_tracker has no record_call() method; cost not recorded.")
    except Exception as exc:
        logger.error("cost_tracker.record_call() failed: %s", exc)


def _check_budget(cost_tracker: Any, branch_id: str | None) -> None:
    """
    Ask the cost_tracker whether the budget cap has been exceeded.

    Uses the correct attribute names from ``orchestrator.cost_tracker.CostTracker``:
      - ``is_exhausted``  (property, bool) for task-level cap
      - ``branch_remaining(branch_id)``  (method, float) for branch-level cap
      - ``total_spent``   (float) — NOT ``total_spent_usd``
      - ``task_budget``   (float) — NOT ``task_budget_usd``

    Raises:
        BudgetExhaustedError: If any cap is exceeded.
    """
    if cost_tracker is None:
        return

    try:
        # Task-level cap: use the ``is_exhausted`` property (not a method)
        is_exhausted = getattr(cost_tracker, "is_exhausted", False)
        if is_exhausted:
            spent = getattr(cost_tracker, "total_spent", 0.0)
            cap = getattr(cost_tracker, "task_budget", 400.0)
            raise BudgetExhaustedError(cap_usd=cap, spent_usd=spent)

        # Branch-level cap: use branch_remaining(branch_id)
        if branch_id is not None:
            branch_remaining_fn = getattr(cost_tracker, "branch_remaining", None)
            if callable(branch_remaining_fn) and branch_remaining_fn(branch_id) <= 0:
                spent = getattr(cost_tracker, "total_spent", 0.0)
                cap = getattr(cost_tracker, "branch_hard_cap", 75.0)
                raise BudgetExhaustedError(cap_usd=cap, spent_usd=spent)
    except BudgetExhaustedError:
        raise
    except Exception as exc:
        logger.warning("Budget check failed (non-fatal): %s", exc)


# ─── Context size guard ───────────────────────────────────────────────────────


def _assert_context_size(
    messages: list[dict[str, Any]],
    model_id: ModelID,
    max_context_tokens: int,
) -> None:
    """
    Estimate the total token count of *messages* and raise
    ``ContextTooLargeError`` if it exceeds *max_context_tokens*.
    """
    estimated = _estimate_messages_tokens(messages, model_id)
    if estimated > max_context_tokens:
        raise ContextTooLargeError(
            estimated_tokens=estimated,
            max_tokens=max_context_tokens,
        )
    logger.debug(
        "Context size OK: ~%d tokens (max=%d)", estimated, max_context_tokens
    )


# ─── Main entry point ─────────────────────────────────────────────────────────


async def spawn_model(
    task_type: TaskType,
    context: dict[str, Any],
    output_schema: type[BaseModel],
    max_tokens: int | None = None,  # BUG-005 fix: None sentinel replaces fragile 4096 magic value
    use_batch: bool = False,
    branch_id: str | None = None,
    db: Any = None,  # asyncpg.Pool
    cost_tracker: Any = None,  # CostTracker instance
    config: AppConfig | None = None,
) -> tuple[BaseModel, AISession]:
    """
    Single entry point for ALL AI calls in the Mariana system.

    This function is the only place in the codebase that talks to an LLM.
    Everything else — orchestrator, tribunal, watchdog, report engine — calls
    this function.

    Args:
        task_type: Determines model routing and prompt construction.
        context: Task-specific input data.  Required keys vary by task_type;
            see ``prompt_builder`` module documentation.
        output_schema: Pydantic ``BaseModel`` subclass.  The model's response
            will be validated against this schema.
        max_tokens: Override the default output token limit.  Pass None (default)
            to use the routing table default.  Any explicit integer is honoured
            (capped at 2× the routing table value).
        use_batch: Ignored in this implementation (batch path not yet wired).
            Kept for API compatibility.
        branch_id: Optional branch identifier for per-branch cost accounting.
        db: Optional asyncpg connection pool.  If provided, the AISession
            record is persisted to the ``ai_sessions`` table.
        cost_tracker: Optional CostTracker instance.  If provided, budget
            caps are enforced and costs are recorded.
        config: AppConfig instance.  If None, ``load_config()`` is called.

    Returns:
        A 2-tuple ``(parsed_output, session)`` where:
        - ``parsed_output`` is a validated instance of ``output_schema``.
        - ``session`` is an :class:`AISession` record with full accounting.

    Raises:
        BudgetExhaustedError: Budget cap exceeded before the call.
        ContextTooLargeError: Assembled prompt exceeds max context tokens.
        ModelCallError: API unreachable / error after retries.
        OutputParseError: Response unparseable after the one-shot repair retry.
    """
    started_at = datetime.now(timezone.utc)
    start_mono = time.monotonic()

    # ── Load config ────────────────────────────────────────────────────────────
    if config is None:
        config = load_config()

    # ── Pre-call budget check ─────────────────────────────────────────────────
    _check_budget(cost_tracker, branch_id)

    # ── Step 1: model routing ─────────────────────────────────────────────────
    model_cfg: ModelConfig = await get_model_config(task_type, config)

    # BUG-005 fix: use None as sentinel instead of the magic value 4096.
    # A caller that genuinely wants 4096 tokens for a task whose routing table
    # default is 2048 would previously have their override silently discarded.
    effective_max_tokens = (
        model_cfg.max_tokens
        if max_tokens is None
        else min(max_tokens, model_cfg.max_tokens * 2)  # allow double, but cap
    )

    logger.info(
        "spawn_model: task=%s model=%s max_tokens=%d",
        task_type.value,
        model_cfg.model_id.value,
        effective_max_tokens,
    )

    # ── Step 2: prompt construction ───────────────────────────────────────────
    messages = build_messages(
        task_type=task_type,
        context=context,
        output_schema=output_schema,
        config=config,
        model_id=model_cfg.model_id,
    )

    # ── Step 2b: context size guard ───────────────────────────────────────────
    max_ctx = getattr(config, "AI_SESSION_MAX_TOKENS_CONTEXT", 40_000)
    _assert_context_size(messages, model_cfg.model_id, max_ctx)

    # ── Step 3: call LLM Gateway (with retry) ─────────────────────────────────
    response_json = await _call_gateway_with_retry(
        messages=messages,
        model_config=model_cfg,
        max_tokens=effective_max_tokens,
        config=config,
    )

    raw_content = _extract_response_content(response_json)
    usage = _extract_usage(response_json)

    # ── Step 4: parse output ──────────────────────────────────────────────────
    parse_error: OutputParseError | None = None

    try:
        parsed_output = parse_output(raw_content, output_schema)
    except OutputParseError as exc:
        parse_error = exc
        logger.warning(
            "OutputParseError on first attempt (task=%s): %s — retrying with hint",
            task_type.value,
            exc,
        )

    if parse_error is not None:
        # Inject error hint as a follow-up user message and retry once.
        error_hint = build_error_hint(parse_error, output_schema)
        repair_messages = list(messages) + [
            {"role": "assistant", "content": raw_content},
            {"role": "user", "content": error_hint},
        ]

        repair_response = await _call_gateway_with_retry(
            messages=repair_messages,
            model_config=model_cfg,
            max_tokens=effective_max_tokens,
            config=config,
        )
        repair_content = _extract_response_content(repair_response)
        repair_usage = _extract_usage(repair_response)

        # Accumulate token counts from both calls.
        usage["prompt_tokens"] += repair_usage["prompt_tokens"]
        usage["completion_tokens"] += repair_usage["completion_tokens"]
        usage["cache_creation_tokens"] += repair_usage["cache_creation_tokens"]
        usage["cache_read_tokens"] += repair_usage["cache_read_tokens"]

        # This may re-raise OutputParseError — which propagates to the caller.
        parsed_output = parse_output(repair_content, output_schema)

    # ── Step 5: compute cost ──────────────────────────────────────────────────
    cost_usd = _compute_cost(
        model_id=model_cfg.model_id,
        input_tokens=usage["prompt_tokens"],
        output_tokens=usage["completion_tokens"],
        cache_creation_tokens=usage["cache_creation_tokens"],
        cache_read_tokens=usage["cache_read_tokens"],
    )

    # ── Step 6: build AISession ───────────────────────────────────────────────
    duration_ms = int((time.monotonic() - start_mono) * 1000)

    session = AISession(
        id=str(uuid.uuid4()),
        task_id=context.get("task_id", "unknown"),
        branch_id=branch_id,
        task_type=task_type,
        model_used=model_cfg.model_id,
        input_tokens=usage["prompt_tokens"],
        output_tokens=usage["completion_tokens"],
        cache_creation_tokens=usage["cache_creation_tokens"],
        cache_read_tokens=usage["cache_read_tokens"],
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        used_batch_api=False,  # synchronous path only in this implementation
        cache_hit=usage["cache_read_tokens"] > 0,
        started_at=started_at,
        error=None,
    )

    logger.info(
        "spawn_model complete: task=%s model=%s tokens_in=%d tokens_out=%d "
        "cost=$%.6f duration=%dms",
        task_type.value,
        model_cfg.model_id.value,
        session.input_tokens,
        session.output_tokens,
        session.cost_usd,
        session.duration_ms,
    )

    # ── Step 7: persist session ───────────────────────────────────────────────
    if db is not None:
        await _persist_session(db, session)

    # ── Step 8: record cost ───────────────────────────────────────────────────
    if cost_tracker is not None:
        _record_cost(cost_tracker, session, branch_id)

    return parsed_output, session
