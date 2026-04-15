"""
Mariana Computer — configuration dataclass.

All tuneable parameters live here.  Runtime values are read from environment
variables (or a .env file) by ``load_config()``.  Hard-coded defaults match
the architecture spec exactly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path  # BUG-028: Required for load_config type annotation

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
    # BUG-010: Score is on 0-1 scale (matching EvaluationOutput.score constraint).
    # ------------------------------------------------------------------
    SCORE_KILL_THRESHOLD: float = 0.4
    SCORE_DEEPEN_THRESHOLD: float = 0.7
    SCORE_TRIBUNAL_THRESHOLD: float = 0.8
    SCORE_PIVOT_AFTER_TWO_CYCLES: float = 0.4

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
    # Timer system
    # ------------------------------------------------------------------
    duration_hours: float = 2.0

    # ------------------------------------------------------------------
    # Infrastructure — Postgres
    # ------------------------------------------------------------------
    POSTGRES_DSN: str = ""
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

    def __post_init__(self) -> None:
        """Validate budget ordering invariants at startup."""
        if not (self.BUDGET_BRANCH_INITIAL <= self.BUDGET_BRANCH_HARD_CAP <= self.BUDGET_TASK_HARD_CAP):
            raise RuntimeError(
                f"Budget validation failed: BUDGET_BRANCH_INITIAL ({self.BUDGET_BRANCH_INITIAL}) "
                f"<= BUDGET_BRANCH_HARD_CAP ({self.BUDGET_BRANCH_HARD_CAP}) "
                f"<= BUDGET_TASK_HARD_CAP ({self.BUDGET_TASK_HARD_CAP}) must hold."
            )
        # BUG-NEW-13 fix: validate that grant amounts are below the hard cap
        # so the hardcoded constants in branch_manager.py stay consistent with
        # whatever values the operator configures via environment variables.
        if not (self.BUDGET_BRANCH_GRANT_SCORE7 < self.BUDGET_BRANCH_HARD_CAP):
            raise RuntimeError(
                f"Budget validation failed: BUDGET_BRANCH_GRANT_SCORE7 "
                f"({self.BUDGET_BRANCH_GRANT_SCORE7}) must be less than "
                f"BUDGET_BRANCH_HARD_CAP ({self.BUDGET_BRANCH_HARD_CAP})."
            )
        if not (self.BUDGET_BRANCH_GRANT_SCORE8 < self.BUDGET_BRANCH_HARD_CAP):
            raise RuntimeError(
                f"Budget validation failed: BUDGET_BRANCH_GRANT_SCORE8 "
                f"({self.BUDGET_BRANCH_GRANT_SCORE8}) must be less than "
                f"BUDGET_BRANCH_HARD_CAP ({self.BUDGET_BRANCH_HARD_CAP})."
            )

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
    # External tool APIs
    # ------------------------------------------------------------------
    PERPLEXITY_API_KEY: str = ""
    NANOBANANA_API_KEY: str = ""
    VEO_API_KEY: str = ""
    QUARTR_API_KEY: str = ""
    FACTSET_API_KEY: str = ""

    # ------------------------------------------------------------------
    # Batch API
    # ------------------------------------------------------------------
    BATCH_POLL_INTERVAL_SECONDS: int = 300

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------
    ADMIN_SECRET_KEY: str = ""  # Set in env to enable /api/shutdown auth (BUG-009)

    # ------------------------------------------------------------------
    # Stripe — billing integration
    # ------------------------------------------------------------------
    STRIPE_SECRET_KEY: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    # ------------------------------------------------------------------
    # Supabase — used by backend for REST API calls (webhooks, admin)
    # ------------------------------------------------------------------
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_KEY: str = ""

    # ------------------------------------------------------------------
    # CORS — BUG-NEW-06: field added so _get_cors_origins() in api.py can
    # read origins from config rather than the dead-code branch being
    # permanently unreachable.
    # Value is a comma-separated string; api.py splits it into a list.
    # An empty string means "use the hardcoded _DEFAULT_CORS_ORIGINS".
    # ------------------------------------------------------------------
    CORS_ALLOWED_ORIGINS: str = ""


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

    # Require POSTGRES_PASSWORD if POSTGRES_DSN is not explicitly set.
    postgres_dsn = os.environ.get("POSTGRES_DSN")
    if not postgres_dsn:
        pg_password = os.environ.get("POSTGRES_PASSWORD")
        if not pg_password:
            raise RuntimeError(
                "POSTGRES_PASSWORD environment variable is required when POSTGRES_DSN is not set. "
                "Set POSTGRES_DSN or POSTGRES_PASSWORD in your environment or .env file."
            )
        # BUG-051: Make host, port, user, and db configurable via env vars
        pg_user = os.environ.get("POSTGRES_USER", "mariana")
        pg_host = os.environ.get("POSTGRES_HOST", "postgresql")
        pg_port = os.environ.get("POSTGRES_PORT", "5432")
        pg_db = os.environ.get("POSTGRES_DB", "mariana")
        postgres_dsn = f"postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_db}"

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
        # BUG-010: 0–1 scale thresholds
        SCORE_KILL_THRESHOLD=_float("SCORE_KILL_THRESHOLD", 0.4),
        SCORE_DEEPEN_THRESHOLD=_float("SCORE_DEEPEN_THRESHOLD", 0.7),
        SCORE_TRIBUNAL_THRESHOLD=_float("SCORE_TRIBUNAL_THRESHOLD", 0.8),
        SCORE_PIVOT_AFTER_TWO_CYCLES=_float("SCORE_PIVOT_AFTER_TWO_CYCLES", 0.4),
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
        POSTGRES_DSN=postgres_dsn,
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
        # External tool APIs
        PERPLEXITY_API_KEY=_str("PERPLEXITY_API_KEY", ""),
        NANOBANANA_API_KEY=_str("NANOBANANA_API_KEY", ""),
        VEO_API_KEY=_str("VEO_API_KEY", ""),
        QUARTR_API_KEY=_str("QUARTR_API_KEY", ""),
        FACTSET_API_KEY=_str("FACTSET_API_KEY", ""),
        # Batch
        BATCH_POLL_INTERVAL_SECONDS=_int("BATCH_POLL_INTERVAL_SECONDS", 300),
        # Timer
        duration_hours=_float("DURATION_HOURS", 2.0),
        # Logging
        LOG_LEVEL=_str("LOG_LEVEL", "INFO"),
        LOG_JSON=_bool("LOG_JSON", True),
        ADMIN_SECRET_KEY=_str("ADMIN_SECRET_KEY", ""),
        CORS_ALLOWED_ORIGINS=_str("CORS_ALLOWED_ORIGINS", ""),
        # Stripe
        STRIPE_SECRET_KEY=_str("STRIPE_SECRET_KEY", ""),
        STRIPE_PUBLISHABLE_KEY=_str("STRIPE_PUBLISHABLE_KEY", ""),
        STRIPE_WEBHOOK_SECRET=_str("STRIPE_WEBHOOK_SECRET", ""),
        # Supabase
        SUPABASE_URL=_str("SUPABASE_URL", ""),
        SUPABASE_ANON_KEY=_str("SUPABASE_ANON_KEY", ""),
        SUPABASE_SERVICE_KEY=_str("SUPABASE_SERVICE_KEY", ""),
    )
