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
from mariana.data.models import ModelID, QualityTier, TaskType
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
    # BUG-014 fix: Increase max_tokens for tasks that produce large JSON.
    # Claude and Gemini write verbose hypotheses (6+ with translations) and
    # evidence extraction outputs that routinely exceed 4096 output tokens,
    # causing truncated JSON and parse failures.
    TaskType.RESEARCH_ARCHITECTURE: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=8192,
        temperature=0.5,
        use_batch=False,
    ),
    TaskType.HYPOTHESIS_GENERATION: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=8192,
        temperature=0.7,
        use_batch=False,
    ),
    TaskType.EVIDENCE_EXTRACTION: ModelConfig(
        model_id=ModelID.DEEPSEEK_CHAT,
        max_tokens=8192,  # BUG-020: evidence outputs exceed 4096 for complex topics
        temperature=0.1,
        use_batch=False,
    ),
    TaskType.EVALUATION: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=8192,  # BUG-020: evaluation verdicts with translations exceed 4096
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
        max_tokens=4096,
        temperature=0.2,
        use_batch=False,
    ),
    TaskType.COMPRESSION: ModelConfig(
        model_id=ModelID.SONNET_46,
        max_tokens=4096,
        temperature=0.1,
        use_batch=True,
    ),
    TaskType.TRIBUNAL_PLAINTIFF: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=8192,  # BUG-020: tribunal outputs with translations exceed 4096
        temperature=0.4,
        use_batch=True,
    ),
    TaskType.TRIBUNAL_DEFENDANT: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=8192,  # BUG-020
        temperature=0.4,
        use_batch=True,
    ),
    TaskType.TRIBUNAL_REBUTTAL: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=8192,  # BUG-020
        temperature=0.4,
        use_batch=True,
    ),
    TaskType.TRIBUNAL_COUNTER: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=8192,  # BUG-020
        temperature=0.4,
        use_batch=True,
    ),
    TaskType.TRIBUNAL_JUDGE: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=8192,  # BUG-020
        temperature=0.2,
        use_batch=True,
    ),
    TaskType.SKEPTIC_QUESTIONS: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=16384,  # BUG-020: skeptic produces massive bilingual Q&A JSON
        temperature=0.5,
        use_batch=False,
    ),
    TaskType.REPORT_DRAFT: ModelConfig(
        model_id=ModelID.SONNET_46,
        max_tokens=16384,  # BUG-020: reports are the largest outputs
        temperature=0.4,
        use_batch=True,
    ),
    TaskType.REPORT_FINAL_EDIT: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=16384,  # BUG-020: final edited reports need full space
        temperature=0.3,
        use_batch=False,
    ),
    TaskType.WATCHDOG: ModelConfig(
        model_id=ModelID.HAIKU_45,
        max_tokens=512,
        temperature=0.1,
        use_batch=False,
    ),
    # ── Intelligence Engine task types ────────────────────────────────────────
    TaskType.CLAIM_EXTRACTION: ModelConfig(
        model_id=ModelID.HAIKU_45,
        max_tokens=4096,
        temperature=0.1,
        use_batch=False,
    ),
    TaskType.SOURCE_CREDIBILITY: ModelConfig(
        model_id=ModelID.HAIKU_45,
        max_tokens=1024,
        temperature=0.1,
        use_batch=False,
    ),
    TaskType.CONTRADICTION_DETECTION: ModelConfig(
        model_id=ModelID.SONNET_46,
        max_tokens=8192,
        temperature=0.2,
        use_batch=False,
    ),
    TaskType.REPLAN: ModelConfig(
        model_id=ModelID.SONNET_46,
        max_tokens=4096,
        temperature=0.3,
        use_batch=False,
    ),
    TaskType.BAYESIAN_UPDATE: ModelConfig(
        model_id=ModelID.SONNET_46,
        max_tokens=4096,
        temperature=0.2,
        use_batch=False,
    ),
    TaskType.GAP_DETECTION: ModelConfig(
        model_id=ModelID.SONNET_46,
        max_tokens=4096,
        temperature=0.3,
        use_batch=False,
    ),
    TaskType.PERSPECTIVE_SYNTHESIS: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=8192,
        temperature=0.5,
        use_batch=False,
    ),
    TaskType.META_SYNTHESIS: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=8192,
        temperature=0.3,
        use_batch=False,
    ),
    TaskType.RETRIEVAL_STRATEGY: ModelConfig(
        model_id=ModelID.HAIKU_45,
        max_tokens=2048,
        temperature=0.1,
        use_batch=False,
    ),
    TaskType.REASONING_AUDIT: ModelConfig(
        model_id=ModelID.OPUS_46,
        max_tokens=8192,
        temperature=0.2,
        use_batch=False,
    ),
    TaskType.EXECUTIVE_SUMMARY: ModelConfig(
        model_id=ModelID.SONNET_46,
        max_tokens=8192,
        temperature=0.3,
        use_batch=False,
    ),
    # ── Fast path (instant / quick tiers) ─────────────────────────────────────
    TaskType.FAST_PATH: ModelConfig(
        model_id=ModelID.GPT4O_MINI,
        max_tokens=4096,
        temperature=0.5,
        use_batch=False,
    ),
}

# ─── Quality-tier routing ────────────────────────────────────────────────────

# Quality tier → model mapping per task category.
# Categories: orchestrator, research, analysis, writing, cheap
_TIER_ROUTING: dict[QualityTier, dict[str, ModelID]] = {
    QualityTier.MAXIMUM: {
        "orchestrator": ModelID.OPUS_46,
        "research": ModelID.OPUS_46,
        "analysis": ModelID.OPUS_46,
        "writing": ModelID.OPUS_46,
        "cheap": ModelID.SONNET_46,
    },
    QualityTier.HIGH: {
        "orchestrator": ModelID.GEMINI_31_PRO,
        "research": ModelID.OPUS_46,
        "analysis": ModelID.GEMINI_31_PRO,
        "writing": ModelID.SONNET_46,
        "cheap": ModelID.HAIKU_45,
    },
    QualityTier.BALANCED: {
        "orchestrator": ModelID.SONNET_46,
        "research": ModelID.SONNET_46,
        "analysis": ModelID.DEEPSEEK_CHAT,
        "writing": ModelID.SONNET_46,
        "cheap": ModelID.DEEPSEEK_CHAT,
    },
    QualityTier.ECONOMY: {
        "orchestrator": ModelID.DEEPSEEK_CHAT,
        "research": ModelID.DEEPSEEK_CHAT,
        "analysis": ModelID.DEEPSEEK_CHAT,
        "writing": ModelID.GPT4O_MINI,
        "cheap": ModelID.GPT4O_MINI,
    },
}

# Map TaskType → category string for tier routing.
_TASK_CATEGORY: dict[TaskType, str] = {
    TaskType.RESEARCH_ARCHITECTURE: "orchestrator",
    TaskType.HYPOTHESIS_GENERATION: "orchestrator",
    TaskType.EVIDENCE_EXTRACTION: "research",
    TaskType.EVALUATION: "analysis",
    TaskType.TRANSLATION: "cheap",
    TaskType.SUMMARIZATION: "writing",
    TaskType.COMPRESSION: "cheap",
    TaskType.TRIBUNAL_PLAINTIFF: "analysis",
    TaskType.TRIBUNAL_DEFENDANT: "analysis",
    TaskType.TRIBUNAL_REBUTTAL: "analysis",
    TaskType.TRIBUNAL_COUNTER: "analysis",
    TaskType.TRIBUNAL_JUDGE: "orchestrator",
    TaskType.SKEPTIC_QUESTIONS: "orchestrator",
    TaskType.REPORT_DRAFT: "writing",
    TaskType.REPORT_FINAL_EDIT: "writing",
    TaskType.WATCHDOG: "cheap",
    # Intelligence Engine
    TaskType.CLAIM_EXTRACTION: "cheap",
    TaskType.SOURCE_CREDIBILITY: "cheap",
    TaskType.CONTRADICTION_DETECTION: "analysis",
    TaskType.REPLAN: "analysis",
    TaskType.BAYESIAN_UPDATE: "analysis",
    TaskType.GAP_DETECTION: "analysis",
    TaskType.PERSPECTIVE_SYNTHESIS: "orchestrator",
    TaskType.META_SYNTHESIS: "orchestrator",
    TaskType.RETRIEVAL_STRATEGY: "cheap",
    TaskType.REASONING_AUDIT: "orchestrator",
    TaskType.EXECUTIVE_SUMMARY: "writing",
    TaskType.FAST_PATH: "cheap",
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

    # BUG-010 fix: route health probe through LLM Gateway (same endpoint
    # used for all model calls) instead of direct DeepSeek API.
    _PING_TIMEOUT_SECONDS: float = 10.0

    def __init__(self) -> None:
        self._state = _HealthState()

    async def is_healthy(self, api_key: str, gateway_base_url: str = "", gateway_api_key: str = "") -> bool:
        """
        Return True if DeepSeek is reachable via the LLM Gateway.

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
            result = await self._ping(gateway_base_url, gateway_api_key)
            self._state.healthy = result
            self._state.last_checked_at = time.monotonic()
            logger.info("DeepSeek health probe result: healthy=%s", result)
            return result

    async def _ping(self, gateway_base_url: str, gateway_api_key: str) -> bool:
        """
        Send a minimal chat completion request through LLM Gateway.

        We ask for exactly 1 token so the cost is negligible.
        Any 2xx response counts as healthy; timeouts or 5xx = unhealthy.
        """
        # M-03 fix: refuse plain-HTTP LLM Gateway URLs in production.
        _gbl = gateway_base_url.rstrip("/").lower()
        if not _gbl.startswith("https://"):
            if not any(
                tok in _gbl
                for tok in ("://localhost", "://127.", "://[::1]", ".local:", ".local/")
            ):
                logger.warning("llm_gateway_base_url_not_https", url=gateway_base_url)
                return False
        url = f"{gateway_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": "deepseek-v3.2",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        headers = {
            "Authorization": f"Bearer {gateway_api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._PING_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=headers,
                )
                if 200 <= response.status_code < 300:
                    return True
                logger.warning(
                    "DeepSeek health ping via gateway returned HTTP %s (not 2xx — treating as unhealthy)",
                    response.status_code,
                )
                return False
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            logger.warning("DeepSeek health ping via gateway failed: %s", exc)
            return False


# Module-level singleton — shared across all calls in the same process.
_deepseek_health_cache = DeepSeekHealthCache()

# ─── Public API ──────────────────────────────────────────────────────────────


async def get_model_config(
    task_type: TaskType,
    config: AppConfig,
    quality_tier: QualityTier | None = None,
) -> ModelConfig:
    """
    Resolve the ModelConfig for *task_type*, applying:

    1. Runtime override from ``config`` (env var ``MODEL_OVERRIDE_{TASK_TYPE}``).
    2. Quality-tier routing (if *quality_tier* is provided and no runtime override).
    3. ROUTING_TABLE lookup (static fallback when no tier is specified).
    4. DeepSeek health gate: if the routed model is a DeepSeek variant and
       the health probe reports unhealthy, downgrade to ``FALLBACK_CHEAP``.

    Args:
        task_type: The AI task being performed.
        config: Loaded AppConfig. Must expose any ``MODEL_OVERRIDE_*`` fields
                and ``DEEPSEEK_API_KEY``.
        quality_tier: Optional user-selected quality tier.  When provided and
                      no runtime override exists, model selection follows
                      ``_TIER_ROUTING`` instead of the static ROUTING_TABLE.

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

    # ── Step 2: quality-tier routing (when tier is provided) ──────────────────
    if quality_tier is not None:
        category = _TASK_CATEGORY.get(task_type)
        if category is not None:
            tier_model_id = _TIER_ROUTING[quality_tier][category]
            # Use the ROUTING_TABLE entry for non-model config (max_tokens, temperature,
            # use_batch); only the model_id is replaced by the tier selection.
            base = ROUTING_TABLE.get(task_type, FALLBACK_CHEAP)
            resolved = ModelConfig(
                model_id=tier_model_id,
                max_tokens=base.max_tokens,
                temperature=base.temperature,
                use_batch=base.use_batch,
            )
            logger.info(
                "Quality-tier routing applied: task=%s tier=%s model=%s",
                task_type.value,
                quality_tier.value,
                tier_model_id.value,
            )
        else:
            logger.warning(
                "No category mapping for task_type=%s; falling back to ROUTING_TABLE.",
                task_type.value,
            )
            resolved = ROUTING_TABLE.get(task_type, FALLBACK_CHEAP)
    else:
        # ── Step 3: static routing table lookup ───────────────────────────────
        resolved = ROUTING_TABLE.get(task_type)
        if resolved is None:
            logger.warning(
                "No routing entry for task_type=%s; using FALLBACK_CHEAP.", task_type.value
            )
            return FALLBACK_CHEAP

    # ── Step 4: DeepSeek health gate (via LLM Gateway) ────────────────────────
    if resolved.model_id in (ModelID.DEEPSEEK_CHAT, ModelID.DEEPSEEK_REASONER):
        gateway_base = getattr(config, "LLM_GATEWAY_BASE_URL", "") or ""
        gateway_key = getattr(config, "LLM_GATEWAY_API_KEY", "") or ""
        if gateway_base and gateway_key:
            healthy = await _deepseek_health_cache.is_healthy(
                api_key="",  # unused now
                gateway_base_url=gateway_base,
                gateway_api_key=gateway_key,
            )
        else:
            # No LLM Gateway configured — treat as unhealthy.
            logger.warning(
                "LLM_GATEWAY not configured; treating DeepSeek as unhealthy for task=%s.",
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
