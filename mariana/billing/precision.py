"""USD ↔ credit quantization helpers (U-02 fix).

Bug U-02 (P3, audit A25 Probe 2): the platform settles credits via
``int(spent_usd * 100)`` in ``mariana/agent/loop.py:_settle_agent_credits``
and ``mariana/main.py:_deduct_user_credits``.  ``int(...)`` truncates
toward zero, so values like ``$0.305`` produce ``30`` credits instead of
the cent-quantized ``31``.  Combined with IEEE-754 float drift
(``0.1 + 0.2 == 0.30000000000000004``) the pre-fix path can under- or
overcharge by 1 credit per task and compounds across long sessions.

This module exposes a single helper, :func:`usd_to_credits`, that converts
any sane USD representation (``Decimal`` / ``float`` / ``str`` / ``int``)
to integer credits using ``decimal.Decimal`` arithmetic with the
``ROUND_HALF_UP`` rounding mode at the cent boundary.

Rounding mode: ``ROUND_HALF_UP``
--------------------------------
Picked over ``ROUND_HALF_EVEN`` (banker's) for two reasons:

1. Predictability — operators reading audit logs see a single, simple
   rule: "values ≥ .5¢ round up, values < .5¢ round down" with no
   look-around-at-the-previous-digit ambiguity.
2. Slight platform-favoring bias on ``.x5`` boundaries.  The expected
   bias is well below 1 credit per *task* on typical traffic, so it
   does not materially change billing fairness — but combined with the
   reservation-floor (100 credits / $1.00) it ensures the platform is
   never structurally undercharged at the rounding step.

The helper is the single canonical conversion point.  Callers MUST NOT
re-implement ``int(x * 100)`` for billing-relevant amounts.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Union

__all__ = ["usd_to_credits"]

# Quantization template — one whole cent-credit, expressed as Decimal('1').
# We multiply USD by 100 first, then quantize to the integer-credit grid;
# this is mathematically equivalent to quantizing to ``Decimal('0.01')``
# in dollars and converting, but more obviously correct as an integer.
_CREDIT_GRID: Decimal = Decimal("1")
_HUNDRED: Decimal = Decimal(100)

UsdLike = Union[Decimal, float, str, int]


def usd_to_credits(usd: UsdLike) -> int:
    """Convert *usd* to integer credits with ROUND_HALF_UP at the cent boundary.

    Parameters
    ----------
    usd:
        USD amount.  Accepted as ``Decimal`` (canonical), ``str`` (explicit
        decimal representation, e.g. ``"0.305"``), ``int``, or ``float``.

        For ``float`` inputs we go through ``str(x)`` first.  Python's
        ``str(float)`` returns the shortest repr that round-trips, so
        ``str(0.305)`` is ``"0.305"`` rather than ``"0.30499999..."``.
        That gives the helper the user's *intended* value rather than
        the IEEE-754 binary representation.  This is critical: feeding
        ``0.305`` directly to ``Decimal()`` would yield
        ``Decimal('0.3049999999999999822...')`` and the wrong answer.

    Returns
    -------
    int
        Integer credits, where 100 credits == $1.00 (the canonical
        platform conversion documented on the Pricing page and used by
        the frontend ``creditsFromUsd`` helper).

    Examples
    --------
    >>> usd_to_credits(Decimal("0.305"))
    31
    >>> usd_to_credits(0.305)        # float — uses str() bridge
    31
    >>> usd_to_credits("0.304")
    30
    >>> usd_to_credits(2)
    200
    """
    if isinstance(usd, Decimal):
        amount = usd
    elif isinstance(usd, bool):  # bool is a subclass of int — be explicit
        amount = Decimal(int(usd))
    elif isinstance(usd, int):
        amount = Decimal(usd)
    elif isinstance(usd, float):
        # str(float) gives the shortest round-trippable repr, which is
        # what the user typed (or what an upstream serializer wrote).
        # Going Decimal(float) directly would expose IEEE-754 drift.
        amount = Decimal(str(usd))
    elif isinstance(usd, str):
        amount = Decimal(usd)
    else:
        raise TypeError(
            f"usd_to_credits: unsupported type {type(usd).__name__}; "
            f"expected Decimal | float | str | int."
        )

    cents = (amount * _HUNDRED).quantize(_CREDIT_GRID, rounding=ROUND_HALF_UP)
    return int(cents)
