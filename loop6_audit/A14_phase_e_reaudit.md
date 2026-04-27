# A14 — Phase E re-audit #9

## Executive summary

I found **1 new issue** that the prior eight re-audits missed.

1. **M-01 [P1] money/correctness | agent tasks reserve credits at 5x the enforced budget and never settle/refund the difference**
   - `POST /api/agent` deducts `max(200, int(body.budget_usd * 500))` credits up front and documents that amount as only a temporary reservation to be "Refunded on error".
   - The agent runtime, however, treats `budget_usd` and `spent_usd` as dollar-denominated values and halts execution once `spent_usd >= budget_usd`.
   - The frontend likewise converts those task fields at **100 credits = $1**, so a `$5` task is shown as a **500-credit** ceiling.
   - I found no completion-time settlement path for agent tasks analogous to the research-task `_deduct_user_credits(...)` flow in `mariana/main.py`; the only refund in the agent path is the narrow insert-failure guard before enqueue.

Read-only audit performed against branch `loop6/zero-bug` at commit `05299a5`. I started from the less-covered Phase E surfaces requested for round #9 (agent/orchestrator internals, background work, frontend security edges, vault, tests, and live Supabase privilege posture), but stopped once M-01 reached HIGH confidence.

---

## M-01 [P1] money/correctness | agent tasks reserve credits at 5x the enforced budget and never settle/refund the difference

- **Severity:** P1
- **Surface:** api / agent billing / frontend budget display
- **File + line numbers:**
  - `mariana/agent/api_routes.py:401-417` — `POST /api/agent` reserves credits with `reserved_credits = max(200, int(body.budget_usd * 500))`
  - `mariana/agent/api_routes.py:413-415` — inline comment says this amount is just a conservative estimate and is "Refunded on error"
  - `mariana/agent/api_routes.py:448-472` — the only refund path is the insert-failure guard before enqueue
  - `mariana/agent/api_routes.py:474-500` — after successful insert/enqueue the handler returns 202 with no settlement hook
  - `mariana/agent/models.py:137-140` — `budget_usd` / `spent_usd` are stored as dollar-denominated task fields
  - `mariana/agent/loop.py:255-260` — runtime budget enforcement halts once `spent_usd >= budget_usd`
  - `mariana/agent/loop.py:478-480`, `507-513`, `610-612` — runtime cost accrual adds dollar costs into `spent_usd`
  - `frontend/src/components/deft/studio/stage.ts:90-95` — frontend canonical conversion is `1 credit == $0.01`
  - `frontend/src/pages/Build.tsx:366-368` — UI displays task budget/spend by converting `budget_usd` / `spent_usd` through that 100-credits-per-dollar helper
  - `mariana/main.py:424-426` — the research-task settlement path also prices credits at `int(total_with_markup * 100)`, reinforcing the platform-wide 100-credits-per-dollar unit

### Reproduction steps

1. Submit an agent task through `POST /api/agent` with any normal budget, e.g. `budget_usd = 5.0`.
2. Observe the submission path deducts `max(200, int(body.budget_usd * 500))` credits before enqueueing. For a `$5` task, that is **2500 credits**.
3. The created `AgentTask` stores `budget_usd=5.0`, and the runtime later checks `_budget_exceeded(task, started_at)` using `if task.spent_usd >= task.budget_usd:`.
4. The only places that increase `task.spent_usd` add LLM/planner dollar costs (`task.spent_usd += cost`). There is no alternate agent-specific "credits" field or 5x multiplier in the runtime.
5. The frontend converts the same task fields with `creditsFromUsd(...)`, whose canonical rule is `1 credit == $0.01`; therefore the UI renders a `$5` task as a **500-credit** ceiling.
6. Search the agent path for post-run reconciliation: unlike the research flow in `mariana/main.py`, there is no completion-time settlement/refund call for reserved agent credits. In `mariana/agent/api_routes.py`, the only refund logic is the narrow `except Exception as insert_exc:` guard around the initial DB insert. Once the task row is inserted successfully, the reservation is never reconciled.

### Impact

This is a deterministic overbilling bug on the common agent-task path. When Supabase credits are enabled, a user is charged **500 credits per budget dollar** up front, but the task itself can only consume **100 credits per budget dollar** before the runtime halts it as budget-exhausted. A `$5` task therefore reserves **2500 credits** while the runtime/UI budget ceiling is only **500 credits**, leaving up to **2000 credits** permanently over-collected.

Because the agent path never settles reserved credits after successful insert/enqueue, the overcharge is not limited to crashes or rare retries. Any ordinary successful task, halted task, or failed task after insert can keep the mismatch. The inline comment explicitly promises the reservation is temporary ("Refunded on error"), but the implemented logic only refunds on pre-enqueue insert failure.

### Recommended fix

1. Unify the unit of account for agent tasks. Pick **one** of these models and apply it end-to-end:
   - `budget_usd` / `spent_usd` are real dollars, so reserve `int(body.budget_usd * 100)` credits; or
   - agent tasks intentionally reserve a higher multiple, but then the runtime/UI must enforce and display the same multiple rather than plain `budget_usd`.
2. Add a mandatory completion-time settlement path for agent tasks, equivalent in spirit to `mariana/main.py:_deduct_user_credits(...)`, so unused reserved credits are automatically refunded and overruns are explicitly reconciled.
3. Add a regression test that starts a `$5` agent task with credit reservation enabled and asserts:
   - initial deduction amount,
   - runtime budget ceiling,
   - final settlement/refund,
   - and the frontend-visible credit ceiling all agree on the same unit conversion.
4. Audit any historical agent submissions created since the reservation change landed and credit back the over-collected delta where the task completed below the reserved amount.

### Confidence

HIGH

---

## Additional round-9 checks completed before stopping

These were reviewed but not promoted to findings once M-01 was confirmed:

- **Agent queue / long-running step surface:** I reviewed `mariana/main.py:738-827`, `mariana/agent/loop.py:342-456`, `mariana/agent/dispatcher.py:114-136`, and `mariana/agent/tools.py:110-149` for stale-task recovery, missing heartbeats, and long-running `code_exec` behavior.
- **Frontend security beyond AuthContext:** I re-checked the previously less-covered agent/build surfaces and the `creditsFromUsd(...)` budget display path, including `frontend/src/pages/Build.tsx` and the shared stage helpers.
- **Vault:** the KDF floor remains enforced in `mariana/vault/router.py` (already inspected during this round before M-01 was confirmed); I did not find a new plaintext/logging regression.
- **Test fixture secrets:** broad repo searches did not surface real `sk_live_`, `pk_live_`, or `sb_secret_` style secrets in tests.
- **Live Supabase privileges:** I queried the connected project `afnbtbeayfkwznhzafay` for `SECURITY DEFINER` execute grants. That check did not change the M-01 conclusion.
