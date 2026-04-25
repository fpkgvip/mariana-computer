"""Deft billing & credit ledger.

Integer-only credit accounting backed by Postgres RPCs:
- grant_credits(user_id, credits, source, ref_type, ref_id, expires_at) -> dict
- spend_credits(user_id, credits, ref_type, ref_id, metadata) -> dict
- refund_credits(user_id, credits, ref_type, ref_id) -> dict
- expire_credits() -> int
- get_my_balance() -> dict (uses auth.uid())

Money invariant: 1 credit = $0.01 (integer, never float).
Per-user serialization is enforced inside each RPC via pg_advisory_xact_lock.
Idempotency is enforced on (ref_type, ref_id) for grants and refunds.
"""

from .ledger import (
    grant_credits,
    spend_credits,
    refund_credits,
    get_balance,
    LedgerError,
    InsufficientBalance,
)
from .quote import estimate_quote, ModelTier, Quote

__all__ = [
    "grant_credits",
    "spend_credits",
    "refund_credits",
    "get_balance",
    "LedgerError",
    "InsufficientBalance",
    "estimate_quote",
    "ModelTier",
    "Quote",
]
