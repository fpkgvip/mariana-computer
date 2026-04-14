"""
mariana/ai/router.py

Model routing for the Mariana AI layer.

Responsibilities:
- Map TaskType → ModelConfig (model_id, max_tokens, temperature, use_batch)
- Provide a DeepSeek health check with a 5-minute TTL cache
- Apply runtime overrides from AppConfig env vars (MODEL_OVERRIDE_{TASK_TYPE})
- Fall back to FALLBACK_CHEAP when DeepSeek is unhealthy and the routed model
  is a DeepSeek model

Only one public function is exported: ``get_model_config()``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

# These are resolved at import time only when the package is installed.
# During parallel builds the sibling modules may not exist yet — callers are
# responsible for ensuring the full package is present before calling.
from mariana.data.models import ModelID, TaskType
from mariana.config import AppConfig

logger = logging.getLogger(__name__)

# ─── Data classes ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelConfig:
    """Resolved routing configuration for a single AI call."""

    model_id: ModelID
    max_tokens: int
    temperature: float
    use_batch: bool = False


# ─── Routing table ───────────────────────────────────────────────────────────

ROUTING_TABLE: dict[TaskType, ModelConfig] = {
    TaskType.HYPOTHESIS_GENERATION: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=4096,
        temperature=0.7,
        use_batch=False,
    ),
    TaskType.EVIDENCE_EXTRACTION: ModelConfig(
        model_id=ModelID.DEEPSEEK_CHAT,
        max_tokens=2048,
        temperature=0.1,
        use_batch=False,
    ),
    TaskType.EVALUATION: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=2048,
        temperature=0.3,
        use_batch=False,
    ),
    TaskType.TRANSLATION: ModelConfig(
        model_id=ModelID.DEEPSEEK_CHAT,
        max_tokens=4096,
        temperature=0.1,
        use_batch=False,
    ),
    TaskType.SUMMARIZATION: ModelConfig(
        model_id=ModelID.SONNET_46,
        max_tokens=2048,
        temperature=0.2,
        use_batch=False,
    ),
    TaskType.COMPRESSION: ModelConfig(
        model_id=ModelID.SONNET_46,
        max_tokens=2048,
        temperature=0.1,
        use_batch=True,
    ),
    TaskType.TRIBUNAL_PLAINTIFF: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=4096,
        temperature=0.4,
        use_batch=True,
    ),
    TaskType.TRIBUNAL_DEFENDANT: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=4096,
        temperature=0.4,
        use_batch=True,
    ),
    TaskType.TRIBUNAL_REBUTTAL: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=3000,
        temperature=0.4,
        use_batch=True,
    ),
    TaskType.TRIBUNAL_COUNTER: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=3000,
        temperature=0.4,
        use_batch=True,
    ),
    TaskType.TRIBUNAL_JUDGE: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=3000,
        temperature=0.2,
        use_batch=True,
    ),
    TaskType.SKEPTIC_QUESTIONS: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=3000,
        temperature=0.5,
        use_batch=False,
    ),
    TaskType.REPORT_DRAFT: ModelConfig(
        model_id=ModelID.SONNET_46,
        max_tokens=8192,
        temperature=0.4,
        use_batch=True,
    ),
    TaskType.REPORT_FINAL_EDIT: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=8192,
        temperature=0.3,
        use_batch=False,
    ),
    TaskType.WATCHDOG: ModelConfig(
        model_id=ModelID.HAIKU_45,
        max_tokens=512,
        temperature=0.1,
        use_batch=False,
    ),
}

# Used when DeepSeek is unhealthy and the originally routed model is DeepSeek.
FALLBACK_CHEAP = ModelConfig(
    model_id=ModelID.GPT4O_MINI,
    max_tokens=2048,
    temperature=0.1,
    use_batch=False,
)

# ─── DeepSeek health cache ───────────────────────────────────────────────────

_DEEPSEEK_HEALTH_TTL_SECONDS: int = 300  # 5 minutes


@dataclass
class _HealthState:
    """Mutable singleton for the DeepSeek health probe result."""

    healthy: bool = True
    last_checked_at: float = 0.0  # epoch seconds
    # BUG-001 fix: do NOT create asyncio.Lock at dataclass/module-import time.
    # A Lock created before an event loop is running can bind to the wrong loop.
    # Use lazy initialisation via get_lock() instead.
    _lock: asyncio.Lock | None = field(default=None, compare=False, repr=False)

    def get_lock(self) -> asyncio.Lock:
        """Return the asyncio.Lock, creating it lazily inside the running loop."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock


class DeepSeekHealthCache:
    """
    Singleton async health probe for the DeepSeek API.

    Uses a 5-minute TTL so we don't bombard the endpoint on every call.
    Thread-safety is achieved with an asyncio.Lock — this is designed for
    cooperative multitasking only (single-process asyncio event loop).

    Usage::

        cache = DeepSeekHealthCache()
        healthy = await cache.is_healthy(api_key="sk-…")
    """

    _PING_URL: str = "https://api.deepseek.com/chat/completions"
    _PING_TIMEOUT_SECONDS: float = 10.0

    def __init__(self) -> None:
        self._state = _HealthState()

    async def is_healthy(self, api_key: str) -> bool:
        """
        Return True if the DeepSeek API is reachable and responding.

        Performs a minimal single-token ping no more than once every 5 minutes.
        The lock prevents duplicate concurrent pings.
        """
        async with self._state.get_lock():
            now = time.monotonic()
            age = now - self._state.last_checked_at

            if age < _DEEPSEEK_HEALTH_TTL_SECONDS:
                logger.debug(
                    "DeepSeek health cache hit: healthy=%s age=%.1fs",
                    self._state.healthy,
                    age,
                )
                return self._state.healthy

            logger.info("DeepSeek health probe — cache expired (age=%.1fs), pinging…", age)
            result = await self._ping(api_key)
            self._state.healthy = result
            self._state.last_checked_at = time.monotonic()
            logger.info("DeepSeek health probe result: healthy=%s", result)
            return result

    async def _ping(self, api_key: str) -> bool:
        """
        Send a minimal chat completion request and check the HTTP status.

        We ask for exactly 1 token so the cost is negligible.
        Any 2xx response counts as healthy; timeouts or 5xx = unhealthy.
        """
        payload = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            # BUG-002 fix: check response.status_code INSIDE the async-with block
            # so any future streaming/body access is still valid.
            async with httpx.AsyncClient(timeout=self._PING_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    self._PING_URL,
                    json=payload,
                    headers=headers,
                )
                if 200 <= response.status_code < 300:
                    return True
                logger.warning(
                    "DeepSeek health ping returned HTTP %s (not 2xx — treating as unhealthy)",
                    response.status_code,
                )
                return False
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            logger.warning("DeepSeek health ping failed: %s", exc)
            return False


# Module-level singleton — shared across all calls in the same process.
_deepseek_health_cache = DeepSeekHealthCache()

# ─── Public API ──────────────────────────────────────────────────────────────


async def get_model_config(
    task_type: TaskType,
    config: AppConfig,
) -> ModelConfig:
    """
    Resolve the ModelConfig for *task_type*, applying:

    1. Runtime override from ``config`` (env var ``MODEL_OVERRIDE_{TASK_TYPE}``).
    2. ROUTING_TABLE lookup.
    3. DeepSeek health gate: if the routed model is a DeepSeek variant and
       the health probe reports unhealthy, downgrade to ``FALLBACK_CHEAP``.

    Args:
        task_type: The AI task being performed.
        config: Loaded AppConfig. Must expose any ``MODEL_OVERRIDE_*`` fields
                and ``DEEPSEEK_API_KEY``.

    Returns:
        A frozen :class:`ModelConfig` ready for use by ``session.spawn_model()``.
    """
    # ── Step 1: check runtime override ────────────────────────────────────────
    # BUG-003 fix: use .upper() so the attr name matches conventional uppercase
    # env-var naming (e.g. MODEL_OVERRIDE_EVIDENCE_EXTRACTION, not _evidence_extraction).
    override_attr = f"MODEL_OVERRIDE_{task_type.value.upper()}"
    override_value: str | None = getattr(config, override_attr, None)

    if override_value:
        try:
            override_model_id = ModelID(override_value)
        except ValueError:
            logger.error(
                "Invalid MODEL_OVERRIDE value '%s' for attr '%s'. Ignoring override.",
                override_value,
                override_attr,
            )
        else:
            # Inherit the rest of the routing config but swap the model.
            base = ROUTING_TABLE.get(task_type, FALLBACK_CHEAP)
            resolved = ModelConfig(
                model_id=override_model_id,
                max_tokens=base.max_tokens,
                temperature=base.temperature,
                use_batch=base.use_batch,
            )
            logger.info(
                "Runtime model override applied: task=%s model=%s",
                task_type.value,
                override_model_id.value,
            )
            return resolved

    # ── Step 2: routing table lookup ──────────────────────────────────────────
    resolved = ROUTING_TABLE.get(task_type)
    if resolved is None:
        logger.warning(
            "No routing entry for task_type=%s; using FALLBACK_CHEAP.", task_type.value
        )
        return FALLBACK_CHEAP

    # ── Step 3: DeepSeek health gate ─────────────────────────────────────────
    if resolved.model_id in (ModelID.DEEPSEEK_CHAT, ModelID.DEEPSEEK_REASONER):
        api_key = getattr(config, "DEEPSEEK_API_KEY", "") or ""
        if api_key:
            healthy = await _deepseek_health_cache.is_healthy(api_key)
        else:
            # No API key configured — treat as unhealthy to avoid 401 failures.
            logger.warning(
                "DEEPSEEK_API_KEY not set; treating DeepSeek as unhealthy for task=%s.",
                task_type.value,
            )
            healthy = False

        if not healthy:
            logger.warning(
                "DeepSeek unhealthy — falling back to %s for task=%s.",
                FALLBACK_CHEAP.model_id.value,
                task_type.value,
            )
            return FALLBACK_CHEAP

    return resolved
