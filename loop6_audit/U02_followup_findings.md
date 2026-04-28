# U-02 follow-up findings

These are NOT new bugs. They are upstream cleanup items intentionally
deferred from the U-02 fix to keep the blast radius minimal. The U-02
bug itself (off-by-one credit truncation at the int-conversion site)
is closed by the central `usd_to_credits` helper.

## F-U02-1 — Push Decimal upstream into `AgentTask.spent_usd`

- Currently `AgentTask.spent_usd: float = 0.0` (`mariana/agent/models.py:145`).
- DB column `agent_tasks.spent_usd` is `DOUBLE PRECISION` (`mariana/agent/schema.sql:20`).
- Storage as float is acceptable — the U-02 bug is not on disk, it is
  at the conversion-to-int site, which is now Decimal-quantized.
- Future work: change the Pydantic field to `Annotated[Decimal, Field(...)]`
  with a JSON encoder/decoder so the wire format is `"0.305"` (string)
  instead of `0.305` (float). Migrate the DB column to `NUMERIC(12,4)`
  in the same change so per-call accumulation is also exact.
- Risk: ~10 read/write callsites for `spent_usd` (see grep in U-02
  plan); each needs an explicit Decimal coercion. Larger blast radius,
  worth its own ticket.

## F-U02-2 — Decimal accumulation in `CostTracker.total_spent`

- `mariana/orchestrator/cost_tracker.py:113` initializes
  `self.total_spent: float = 0.0` and accumulates via `+=` in `record_call`,
  `record_branch_spend`, and `record_raw_spend`.
- IEEE-754 drift compounds across long sessions. The U-02 fix masks
  this at settlement time (Decimal quantization at the boundary), but
  the in-flight log values shown via `total_spent`, `total_with_markup`,
  and the streamed event payloads (`mariana/orchestrator/event_loop.py:652`,
  `654`, `3171`) still carry the float drift.
- Future work: convert `total_spent`, `per_model[*]`, `per_branch[*]`,
  `finalization_spent` to Decimal accumulators; add Decimal type to
  the serialized `CostTrackerModel` (`mariana/data/models.py:553`).
- Pricing tables (`_MODEL_PRICING` in `mariana/ai/session.py`) can stay
  float; conversion at the boundary of `_compute_cost_usd` is enough.

## F-U02-3 — Reservation formula at `mariana/agent/api_routes.py:472`

- Currently `reserved_credits = max(100, int(body.budget_usd * 100))`.
- This is an upfront budget *ceiling*, not a billed amount. The 100-credit
  floor means a sub-cent rounding difference cannot under-reserve the
  user, so this site is not a U-02 bug.
- Future work (cosmetic): swap to
  `reserved_credits = max(100, usd_to_credits(body.budget_usd))` for
  consistency with the settlement sites. Behavior is identical for all
  inputs that pass Pydantic `ge=1.0` validation.

## F-U02-4 — Stripe / billing-page USD displays

- `mariana/api.py:4207` does `f"${evt.get('spent_usd', 0):.4f}"` for a
  display string. Float-format is fine for human display.
- `mariana/api.py:8914` and `total_spent_usd` query/return paths around
  `mariana/api.py:1164`, `3094`, `3725-3779` all stay float — these are
  reporting / analytic surfaces, not billed amounts, and don't go
  through `int(...)`.

## Status

None of these block the U-02 fix. Capture as future tickets if the
team wants Decimal end-to-end across the agent runtime; until then, the
boundary-only Decimal at `usd_to_credits` is sufficient and the
regression suite (`tests/test_u02_decimal_billing.py`) pins the
correct behavior at every billed-conversion site.
