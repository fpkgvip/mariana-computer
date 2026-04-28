# U-02 Fix Report — Float-to-int billing truncation

- **Bug ID:** U-02 (P3)
- **Source:** Phase E re-audit #20 (`loop6_audit/A25_phase_e_reaudit.md` Probe 2)
- **Status:** **FIXED 2026-04-28**
- **Branch:** `loop6/zero-bug`
- **Fix mechanism:** central `usd_to_credits` Decimal helper, ROUND_HALF_UP at cent boundary

## Summary

Both billing settlement paths converted USD totals to integer credits with
`int(x * 100)`, which truncates toward zero. Combined with IEEE-754 float
accumulation in `CostTracker.total_spent` and `AgentTask.spent_usd`, this
under- or over-charged users by up to 1 credit per task on cent boundaries
(e.g. `int(0.305 * 100) == 30` instead of the cent-quantized 31), with
extra drift on long sessions where many sub-cent costs accumulate.

## Root cause callsites

| Location | Pre-fix code | Use |
| --- | --- | --- |
| `mariana/agent/loop.py:523` | `final_tokens = int(task.spent_usd * 100)` | Agent task settlement |
| `mariana/main.py:425` | `final_tokens = int(total_with_markup * 100)` | Legacy investigation settlement |

The reservation site at `mariana/agent/api_routes.py:472`
(`max(100, int(body.budget_usd * 100))`) is left unchanged: that is an
upfront ceiling computation against user-supplied budget, not a billed
amount, and the floor (`max(100, ...)`) means a sub-cent rounding
difference cannot under-reserve below the canonical $1.00 floor. See
followups for the optional unification.

## Fix

New module `mariana/billing/precision.py` exposes a single helper:

```python
from decimal import Decimal, ROUND_HALF_UP

def usd_to_credits(usd) -> int:
    if isinstance(usd, float):
        amount = Decimal(str(usd))    # str() bridge avoids IEEE-754 surprises
    elif isinstance(usd, Decimal):
        amount = usd
    elif isinstance(usd, (int, str)):
        amount = Decimal(usd)
    else:
        raise TypeError(...)
    return int((amount * Decimal(100)).quantize(Decimal("1"),
                                                 rounding=ROUND_HALF_UP))
```

Both billing-relevant callsites import `usd_to_credits` and call it
instead of `int(x * 100)`. The helper accepts `Decimal | float | str | int`
so callers can migrate to Decimal incrementally without churning every
boundary.

### Why the `str(float)` bridge

Going `Decimal(0.305)` directly produces
`Decimal('0.30499999999999999...')` because 0.305 is not representable
in IEEE-754 binary. `str(0.305)` returns `"0.305"` (Python's shortest
round-trippable repr), which then converts cleanly to the user's
intended value.

## Rounding mode: ROUND_HALF_UP

**Picked over ROUND_HALF_EVEN (banker's) and ROUND_DOWN (current bug):**

- `ROUND_HALF_UP` is predictable: "values >= .5¢ round up; values < .5¢
  round down" — operators reading audit logs do not need to know the
  previous digit.
- It introduces a slight platform-favoring bias on `.x5` boundaries.
  Across realistic traffic this is well below 1 credit per task and is
  paired with the 100 credits / $1.00 reservation floor so users never
  see a structural undercharge at the rounding step.
- `ROUND_HALF_EVEN` would be statistically unbiased but is harder to
  reason about during incident review.
- `ROUND_DOWN` is the broken pre-fix behavior; explicitly rejected.

## Scope of change (smallest blast radius)

- `mariana/billing/precision.py` — **new** (helper + docstring rationale)
- `mariana/agent/loop.py` — replace one `int(x*100)` callsite, update
  one docstring line
- `mariana/main.py` — replace one `int(x*100)` callsite

NOT changed:
- `AgentTask.spent_usd: float` model field (DB column is
  `DOUBLE PRECISION`; the bug is the int-conversion truncation, not
  storage; pushing Decimal upstream is a separate, larger change —
  tracked in `U02_followup_findings.md`).
- `CostTracker.total_spent: float` accumulator (same reason).
- `mariana/agent/api_routes.py:472` reservation formula (not a billed
  amount; floor at 100 makes the rounding difference moot).
- `mariana/ai/session.py` per-call cost computation (still float; the
  Decimal conversion happens at the settlement boundary which is the
  only place truncation actually matters).

## TDD record

- **RED tests added:** 5 in `tests/test_u02_decimal_billing.py`
  - `test_30_5_cents_rounds_up` — helper rounds $0.305 → 31
  - `test_no_float_drift_accumulation` — Decimal path is exact for 100×$0.01
  - `test_settlement_uses_quantized_amount` — agent settlement RPC
    payload uses 31, not 30
  - `test_legacy_investigation_quantize` — `_deduct_user_credits` RPC
    payload uses 31, not 30
  - `test_helper_accepts_decimal_float_str_int` — input-type tolerance
- **Verification at HEAD `436d63a`:** all 5 RED (confirmed by
  temporarily stashing the fix; module-not-found and value
  assertions both fail as expected).
- **Verification post-fix:** all 5 GREEN.

## Test count delta

| Before | After | Delta |
| --- | --- | --- |
| 366 passed, 13 skipped | 371 passed, 13 skipped | **+5 passed** |

S-01, T-01, U-01, U-03, M-01 regression suites all still GREEN
(25 / 25 passing in targeted re-run).

## Files changed

- `mariana/billing/precision.py` (new, 106 lines)
- `mariana/agent/loop.py` (1 callsite, 1 docstring tweak)
- `mariana/main.py` (1 callsite)
- `tests/test_u02_decimal_billing.py` (new, 5 tests)
- `loop6_audit/REGISTRY.md` (U-02 → FIXED)
- `loop6_audit/U02_FIX_REPORT.md` (this file)
- `loop6_audit/U02_followup_findings.md` (residual upstream-Decimal work)
