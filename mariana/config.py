"""
Mariana Computer — configuration dataclass.

All tuneable parameters live here.  Runtime values are read from environment
variables (or a .env file) by ``load_config()``.  Hard-coded defaults match
the architecture spec exactly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from mariana.data.models import ModelID


# ---------------------------------------------------------------------------
# Compatibility alias — other modules may import as AppConfig
# ---------------------------------------------------------------------------


@dataclass
class AppConfig:
    """Central configuration object for the Mariana research engine."""

    # ------------------------------------------------------------------
    # Model IDs (architecture-spec defaults)
    # ------------------------------------------------------------------
    MODEL_CHEAP: str = ModelID.DEEPSEEK_CHAT.value
    MODEL_MEDIUM: str = ModelID.SONNET_46.value
    MODEL_EXPENSIVE: str = ModelID.OPUS_46.value
    MODEL_WATCHDOG: str = ModelID.HAIKU_45.value

    # ------------------------------------------------------------------
    # Budget caps (USD) — architecture spec §6.3
    # ------------------------------------------------------------------
    BUDGET_BRANCH_INITIAL: float = 5.00
    BUDGET_BRANCH_GRANT_SCORE7: float = 20.00
    BUDGET_BRANCH_GRANT_SCORE8: float = 50.00
    BUDGET_BRANCH_HARD_CAP: float = 75.00
    BUDGET_TASK_HARD_CAP: float = 400.00

    # ------------------------------------------------------------------
    # Branch scoring thresholds — architecture spec §6.3
    # Score is on 0-10 scale.
    # ------------------------------------------------------------------
    SCORE_KILL_THRESHOLD: float = 4.0
    SCORE_DEEPEN_THRESHOLD: float = 7.0
    SCORE_TRIBUNAL_THRESHOLD: float = 8.0
    SCORE_PIVOT_AFTER_TWO_CYCLES: float = 4.0

    # ------------------------------------------------------------------
    # Cache TTLs (seconds) — architecture spec §6.4
    # ------------------------------------------------------------------
    CACHE_TTL_NEWS: int = 86_400          # 24 hours
    CACHE_TTL_FILINGS: int = 604_800      # 7 days
    CACHE_TTL_GOVERNMENT: int = 604_800   # 7 days
    CACHE_TTL_STATIC: int = 2_592_000     # 30 days
    CACHE_TTL_FLOW: int = 14_400          # 4 hours (options flow, dark pool)
    CACHE_TTL_REFERENCE: int = 86_400     # 24 hours

    # ------------------------------------------------------------------
    # Deduplication — architecture spec §6.4
    # ------------------------------------------------------------------
    QUERY_DEDUP_SIMILARITY_THRESHOLD: float = 0.92

    # ------------------------------------------------------------------
    # Browser pool — architecture spec §8
    # ------------------------------------------------------------------
    BROWSER_POOL_MIN: int = 2
    BROWSER_POOL_MAX: int = 8
    BROWSER_REQUEST_DELAY_MIN: float = 1.0   # seconds
    BROWSER_REQUEST_DELAY_MAX: float = 3.5   # seconds

    # ------------------------------------------------------------------
    # Diminishing returns — architecture spec §6.5
    # ------------------------------------------------------------------
    DIMINISHING_NOVELTY_THRESHOLD: float = 0.10
    DIMINISHING_NEW_SOURCES_THRESHOLD: int = 3
    DIMINISHING_SCORE_DELTA_THRESHOLD: float = 1.0
    DIMINISHING_FLAGS_HALT: int = 3

    # ------------------------------------------------------------------
    # AI session limits — architecture spec §5
    # ------------------------------------------------------------------
    AI_SESSION_MAX_TOKENS_CONTEXT: int = 40_000
    AI_SESSION_MAX_DURATION_SECONDS: int = 300
    AI_MAX_RETRIES: int = 3
    AI_RETRY_BACKOFF_BASE: float = 2.0

    # ------------------------------------------------------------------
    # Watchdog — architecture spec §4
    # ------------------------------------------------------------------
    WATCHDOG_TRIGGER_EVERY_N_CALLS: int = 10

    # ------------------------------------------------------------------
    # Skeptic — architecture spec §7
    # ------------------------------------------------------------------
    SKEPTIC_MAX_OPEN_QUESTIONS_FOR_REPORT: int = 2

    # ------------------------------------------------------------------
    # Checkpoint
    # ------------------------------------------------------------------
    CHECKPOINT_INTERVAL_CYCLES: int = 10

    # ------------------------------------------------------------------
    # Infrastructure — Postgres
    # ------------------------------------------------------------------
    POSTGRES_DSN: str = "postgresql://mariana:mariana@postgresql:5432/mariana"
    POSTGRES_POOL_MIN: int = 2
    POSTGRES_POOL_MAX: int = 10

    # ------------------------------------------------------------------
    # Infrastructure — Redis
    # ------------------------------------------------------------------
    REDIS_URL: str = "redis://redis:6379/0"

    # ------------------------------------------------------------------
    # Filesystem
    # ------------------------------------------------------------------
    DATA_ROOT: str = "/data/mariana"

    @property
    def checkpoints_dir(self) -> str:
        return f"{self.DATA_ROOT}/checkpoints"

    @property
    def reports_dir(self) -> str:
        return f"{self.DATA_ROOT}/reports"

    @property
    def findings_dir(self) -> str:
        return f"{self.DATA_ROOT}/findings"

    @property
    def inbox_dir(self) -> str:
        return f"{self.DATA_ROOT}/inbox"

    # ------------------------------------------------------------------
    # API keys & external service credentials
    # ------------------------------------------------------------------
    LLM_GATEWAY_API_KEY: str = ""
    LLM_GATEWAY_BASE_URL: str = "https://api.llmgateway.io/v1"
    POLYGON_API_KEY: str = ""
    UNUSUAL_WHALES_API_KEY: str = ""
    FRED_API_KEY: str = ""
    DEEPSEEK_API_KEY: str = ""

    # ------------------------------------------------------------------
    # Batch API
    # ------------------------------------------------------------------
    BATCH_POLL_INTERVAL_SECONDS: int = 300

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True


# Alias for backward compat
Config = AppConfig


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def load_config(env_file: str | Path | None = None) -> AppConfig:
    """
    Build an AppConfig instance populated from environment variables.
    """
    if env_file is not None:
        load_dotenv(dotenv_path=env_file, override=False)
    else:
        load_dotenv(override=False)

    def _str(key: str, default: str) -> str:
        return os.environ.get(key, default)

    def _int(key: str, default: int) -> int:
        raw = os.environ.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def _float(key: str, default: float) -> float:
        raw = os.environ.get(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def _bool(key: str, default: bool) -> bool:
        raw = os.environ.get(key)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    pg_password = _str("POSTGRES_PASSWORD", "mariana")

    return AppConfig(
        # Model tiers
        MODEL_CHEAP=_str("MODEL_CHEAP", ModelID.DEEPSEEK_CHAT.value),
        MODEL_MEDIUM=_str("MODEL_MEDIUM", ModelID.SONNET_46.value),
        MODEL_EXPENSIVE=_str("MODEL_EXPENSIVE", ModelID.OPUS_46.value),
        MODEL_WATCHDOG=_str("MODEL_WATCHDOG", ModelID.HAIKU_45.value),
        # Budget
        BUDGET_BRANCH_INITIAL=_float("BUDGET_BRANCH_INITIAL", 5.00),
        BUDGET_BRANCH_GRANT_SCORE7=_float("BUDGET_BRANCH_GRANT_SCORE7", 20.00),
        BUDGET_BRANCH_GRANT_SCORE8=_float("BUDGET_BRANCH_GRANT_SCORE8", 50.00),
        BUDGET_BRANCH_HARD_CAP=_float("BUDGET_BRANCH_HARD_CAP", 75.00),
        BUDGET_TASK_HARD_CAP=_float("BUDGET_TASK_HARD_CAP", 400.00),
        # Scoring
        SCORE_KILL_THRESHOLD=_float("SCORE_KILL_THRESHOLD", 4.0),
        SCORE_DEEPEN_THRESHOLD=_float("SCORE_DEEPEN_THRESHOLD", 7.0),
        SCORE_TRIBUNAL_THRESHOLD=_float("SCORE_TRIBUNAL_THRESHOLD", 8.0),
        SCORE_PIVOT_AFTER_TWO_CYCLES=_float("SCORE_PIVOT_AFTER_TWO_CYCLES", 4.0),
        # Cache TTLs
        CACHE_TTL_NEWS=_int("CACHE_TTL_NEWS", 86_400),
        CACHE_TTL_FILINGS=_int("CACHE_TTL_FILINGS", 604_800),
        CACHE_TTL_GOVERNMENT=_int("CACHE_TTL_GOVERNMENT", 604_800),
        CACHE_TTL_STATIC=_int("CACHE_TTL_STATIC", 2_592_000),
        CACHE_TTL_FLOW=_int("CACHE_TTL_FLOW", 14_400),
        CACHE_TTL_REFERENCE=_int("CACHE_TTL_REFERENCE", 86_400),
        # Dedup
        QUERY_DEDUP_SIMILARITY_THRESHOLD=_float("QUERY_DEDUP_SIMILARITY_THRESHOLD", 0.92),
        # Browser
        BROWSER_POOL_MIN=_int("BROWSER_POOL_MIN", 2),
        BROWSER_POOL_MAX=_int("BROWSER_POOL_MAX", 8),
        BROWSER_REQUEST_DELAY_MIN=_float("BROWSER_REQUEST_DELAY_MIN", 1.0),
        BROWSER_REQUEST_DELAY_MAX=_float("BROWSER_REQUEST_DELAY_MAX", 3.5),
        # Diminishing returns
        DIMINISHING_NOVELTY_THRESHOLD=_float("DIMINISHING_NOVELTY_THRESHOLD", 0.10),
        DIMINISHING_NEW_SOURCES_THRESHOLD=_int("DIMINISHING_NEW_SOURCES_THRESHOLD", 3),
        DIMINISHING_SCORE_DELTA_THRESHOLD=_float("DIMINISHING_SCORE_DELTA_THRESHOLD", 1.0),
        DIMINISHING_FLAGS_HALT=_int("DIMINISHING_FLAGS_HALT", 3),
        # AI
        AI_SESSION_MAX_TOKENS_CONTEXT=_int("AI_SESSION_MAX_TOKENS_CONTEXT", 40_000),
        AI_SESSION_MAX_DURATION_SECONDS=_int("AI_SESSION_MAX_DURATION_SECONDS", 300),
        AI_MAX_RETRIES=_int("AI_MAX_RETRIES", 3),
        AI_RETRY_BACKOFF_BASE=_float("AI_RETRY_BACKOFF_BASE", 2.0),
        # Watchdog
        WATCHDOG_TRIGGER_EVERY_N_CALLS=_int("WATCHDOG_TRIGGER_EVERY_N_CALLS", 10),
        # Skeptic
        SKEPTIC_MAX_OPEN_QUESTIONS_FOR_REPORT=_int("SKEPTIC_MAX_OPEN_QUESTIONS_FOR_REPORT", 2),
        # Checkpoint
        CHECKPOINT_INTERVAL_CYCLES=_int("CHECKPOINT_INTERVAL_CYCLES", 10),
        # Postgres
        POSTGRES_DSN=_str(
            "POSTGRES_DSN",
            f"postgresql://mariana:{pg_password}@postgresql:5432/mariana",
        ),
        POSTGRES_POOL_MIN=_int("POSTGRES_POOL_MIN", 2),
        POSTGRES_POOL_MAX=_int("POSTGRES_POOL_MAX", 10),
        # Redis
        REDIS_URL=_str("REDIS_URL", "redis://redis:6379/0"),
        # Filesystem
        DATA_ROOT=_str("DATA_ROOT", "/data/mariana"),
        # API keys
        LLM_GATEWAY_API_KEY=_str("LLM_GATEWAY_API_KEY", ""),
        LLM_GATEWAY_BASE_URL=_str("LLM_GATEWAY_BASE_URL", "https://api.llmgateway.io/v1"),
        POLYGON_API_KEY=_str("POLYGON_API_KEY", ""),
        UNUSUAL_WHALES_API_KEY=_str("UNUSUAL_WHALES_API_KEY", ""),
        FRED_API_KEY=_str("FRED_API_KEY", ""),
        DEEPSEEK_API_KEY=_str("DEEPSEEK_API_KEY", ""),
        # Batch
        BATCH_POLL_INTERVAL_SECONDS=_int("BATCH_POLL_INTERVAL_SECONDS", 300),
        # Logging
        LOG_LEVEL=_str("LOG_LEVEL", "INFO"),
        LOG_JSON=_bool("LOG_JSON", True),
    )
