"""Task-scoped vault runtime — env injection + outbound redaction.

When the frontend submits a goal that references vaulted secrets via the
``$KEY_NAME`` sentinel grammar, it decrypts them locally and POSTs them to
``/api/agent`` as an ephemeral ``vault_env`` mapping.  The orchestrator
persists that mapping in Redis under ``vault:env:{task_id}`` (TTL bounded
by the task's wall-clock budget) and, when the agent loop boots, installs
two task-scoped :class:`contextvars.ContextVar` values:

  • ``vault_env``  — ``dict[str, str]`` available to dispatcher tools so
    the sandbox sees secrets as real environment variables.
  • ``redactor``   — fast string rewriter from
    :func:`mariana.vault.redaction.build_redactor` that masks every
    plaintext occurrence with ``[REDACTED:KEY_NAME]`` before any value
    is logged, streamed, or persisted.

These contextvars are ASYNC-task-local so concurrent agent loops in the
same process never bleed secrets across tasks.

The module never logs secret values and the Redis-side payload is
deleted as soon as the task reaches a terminal state.
"""

from __future__ import annotations

import contextvars
import json
from typing import Any, Callable, Mapping

from mariana.util.redis_url import assert_local_or_tls
from mariana.vault.redaction import build_redactor


# ---------------------------------------------------------------------------
# U-03 fix — Redis transport policy + fail-closed exceptions
# ---------------------------------------------------------------------------


class VaultUnavailableError(RuntimeError):
    """Raised when a task that REQUESTED secrets cannot durably store /
    retrieve them through the vault Redis backing store.

    Callers (API task-creation handler, agent worker pre-tool gate) must
    surface this as a fail-closed error: a 503 to the API client, or a
    task abort with ``error`` set before any tool execution.  Never
    swallow this — the whole point of U-03 is that we refuse to run a
    task that asked for secrets when those secrets cannot be honoured.
    """


def _validate_redis_url_for_vault(url: str | None) -> None:
    """Enforce ``rediss://`` (TLS) for any non-loopback Redis URL."""
    assert_local_or_tls(url, surface="vault env transport")

# Hard caps mirror the frontend grammar so server is the second line of defence.
_MAX_VAULT_ENV_ENTRIES = 50
_MAX_VAULT_VALUE_LEN = 16_384

# Redis key + TTL fudge.  TTL is set by the caller; this is the absolute
# floor we'll allow if a caller passes a bogus value.
REDIS_KEY_FMT = "vault:env:{task_id}"
_MIN_TTL_SECONDS = 600  # 10 min minimum even for a tiny budget

# Default identity redactor used when no secrets are bound.
def _identity(s: str) -> str:
    return s


_vault_env_var: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "deft_vault_env", default={}
)
_redactor_var: contextvars.ContextVar[Callable[[str], str]] = contextvars.ContextVar(
    "deft_vault_redactor", default=_identity
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_vault_env(env: Mapping[str, Any]) -> dict[str, str]:
    """Validate a vault_env payload from the API.

    Raises ``ValueError`` on any malformed entry; the API translates that
    into a 422.  Empty dicts return empty dicts (no-op).
    """
    if not env:
        return {}
    if not isinstance(env, Mapping):
        raise ValueError("vault_env must be an object")
    if len(env) > _MAX_VAULT_ENV_ENTRIES:
        raise ValueError(
            f"vault_env too large: {len(env)} > {_MAX_VAULT_ENV_ENTRIES}"
        )
    out: dict[str, str] = {}
    for name, value in env.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"vault_env: invalid name {name!r}")
        if not isinstance(value, str):
            raise ValueError(f"vault_env: value for {name!r} must be a string")
        if len(value) == 0:
            # Accept and drop — we don't redact empty strings.
            continue
        if len(value) > _MAX_VAULT_VALUE_LEN:
            raise ValueError(f"vault_env: value for {name!r} too long")
        out[name] = value
    return out


import re

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


# ---------------------------------------------------------------------------
# Redis storage
# ---------------------------------------------------------------------------


async def store_vault_env(
    redis: Any,
    task_id: str,
    env: Mapping[str, str],
    *,
    ttl_seconds: int,
    redis_url: str | None = None,
) -> None:
    """Persist a task's vault_env to Redis with a bounded TTL.

    U-03 fix: when ``env`` is non-empty (i.e. the user explicitly
    requested per-task secrets) we **fail closed** on any Redis error —
    raising :class:`VaultUnavailableError` so the API can return 503 and
    the task is never enqueued without its secrets.  When ``env`` is
    empty there is nothing to store and the function is a true no-op,
    preserving backwards compatibility with tasks that don't need a
    vault.

    If ``redis_url`` is supplied, it is validated against the
    rediss://-required policy before any IO is attempted.
    """
    if not env:
        # Nothing to store — never touch Redis (back-compat: a task
        # without vault_env should not need Redis at all).
        return
    # Validate transport BEFORE any IO so we fail loud at task-creation
    # time rather than after a half-finished write.
    _validate_redis_url_for_vault(redis_url)
    if redis is None:
        # The caller asked for vault storage but no client is configured.
        # That is a fail-closed condition.
        raise VaultUnavailableError(
            f"vault_env requested for task {task_id} but no Redis client is configured"
        )
    ttl = max(_MIN_TTL_SECONDS, int(ttl_seconds))
    key = REDIS_KEY_FMT.format(task_id=task_id)
    payload = json.dumps(dict(env))
    try:
        await redis.set(key, payload, ex=ttl)
    except Exception as exc:
        # U-03 fix: do NOT swallow.  Surface as VaultUnavailableError so
        # the route handler can return 503 and the task is rejected
        # before enqueue.
        raise VaultUnavailableError(
            f"vault_env store failed for task {task_id}: {exc}"
        ) from exc


async def fetch_vault_env(
    redis: Any,
    task_id: str,
    *,
    requires_vault: bool = False,
    redis_url: str | None = None,
) -> dict[str, str]:
    """Read back the persisted env.

    U-03 fix: when ``requires_vault`` is True (i.e. the task was
    created with a non-empty ``vault_env`` and depends on secret
    injection to behave correctly), any Redis error OR a missing /
    empty payload raises :class:`VaultUnavailableError` so the agent
    worker fails the task BEFORE invoking any tool.  Returning ``{}``
    in that case would silently strip the user's secrets and let the
    task run as if they had been honoured — exactly the behaviour the
    bug fix exists to prevent.

    For ``requires_vault=False`` the legacy semantics are preserved:
    return ``{}`` on miss/error.  This is the path tasks without a
    vault take (zero new Redis dependency).
    """
    if requires_vault:
        _validate_redis_url_for_vault(redis_url)
    if redis is None:
        if requires_vault:
            raise VaultUnavailableError(
                f"vault_env required for task {task_id} but no Redis client is configured"
            )
        return {}
    key = REDIS_KEY_FMT.format(task_id=task_id)
    try:
        raw = await redis.get(key)
    except Exception as exc:
        if requires_vault:
            raise VaultUnavailableError(
                f"vault_env fetch failed for task {task_id}: {exc}"
            ) from exc
        return {}
    if not raw:
        if requires_vault:
            # The task asked for secrets but Redis has nothing for it
            # (evicted by TTL, evicted by maxmemory, never written
            # because of an earlier silent store_vault_env loss, etc.).
            # Fail closed.
            raise VaultUnavailableError(
                f"vault_env missing for task {task_id} that required vault"
            )
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # Final sanity: enforce caps even on the way out.
    out: dict[str, str] = {}
    for k, v in list(data.items())[:_MAX_VAULT_ENV_ENTRIES]:
        if isinstance(k, str) and isinstance(v, str) and _NAME_RE.match(k):
            out[k] = v[:_MAX_VAULT_VALUE_LEN]
    return out


async def clear_vault_env(redis: Any, task_id: str) -> None:
    """Delete the per-task vault_env blob.  Idempotent."""
    if redis is None:
        return
    try:
        await redis.delete(REDIS_KEY_FMT.format(task_id=task_id))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Context activation
# ---------------------------------------------------------------------------


class TaskContextHandle:
    """Token bundle returned from :func:`set_task_context` so callers can reset."""

    __slots__ = ("_env_token", "_redactor_token")

    def __init__(self, env_token: contextvars.Token[Any], redactor_token: contextvars.Token[Any]) -> None:
        self._env_token = env_token
        self._redactor_token = redactor_token

    def reset(self) -> None:
        try:
            _vault_env_var.reset(self._env_token)
        except Exception:
            _vault_env_var.set({})
        try:
            _redactor_var.reset(self._redactor_token)
        except Exception:
            _redactor_var.set(_identity)


def set_task_context(env: Mapping[str, str]) -> TaskContextHandle:
    """Install env + redactor into the current async context."""
    e = dict(env or {})
    redactor = build_redactor(e) if e else _identity
    e_tok = _vault_env_var.set(e)
    r_tok = _redactor_var.set(redactor)
    return TaskContextHandle(e_tok, r_tok)


def get_task_env() -> dict[str, str]:
    """Return a *copy* of the current vault_env (callers shouldn't mutate)."""
    return dict(_vault_env_var.get())


def get_redactor() -> Callable[[str], str]:
    return _redactor_var.get()


def redact(s: str) -> str:
    """Apply the active redactor to a string (no-op if none installed)."""
    if not isinstance(s, str) or not s:
        return s
    return _redactor_var.get()(s)


def redact_payload(value: Any, *, depth: int = 0) -> Any:
    """Walk a JSON-serialisable payload and redact every string in place.

    Recursion is bounded to depth 32 so a malicious tool result can't
    DoS the redactor with a deeply nested structure.
    """
    if depth > 32:
        return value
    r = _redactor_var.get()
    if r is _identity:
        return value
    if isinstance(value, str):
        return r(value)
    if isinstance(value, dict):
        return {k: redact_payload(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_payload(v, depth=depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(v, depth=depth + 1) for v in value)
    return value


__all__ = [
    "validate_vault_env",
    "store_vault_env",
    "fetch_vault_env",
    "clear_vault_env",
    "set_task_context",
    "get_task_env",
    "get_redactor",
    "redact",
    "redact_payload",
    "TaskContextHandle",
    "REDIS_KEY_FMT",
    "VaultUnavailableError",
    "_validate_redis_url_for_vault",
]
